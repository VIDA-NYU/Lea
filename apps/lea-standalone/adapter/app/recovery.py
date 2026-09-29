"""Durable execution boundaries and conservative crash recovery.

The prover calls ``record`` synchronously. A failed write is fatal to that
activation: no paid request or tool is allowed to follow an unrecorded intent.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from . import db, store, diagnostics, runtime

SCHEMA_VERSION = 1
RETRY_SAFE_TOOLS = frozenset({"read_file", "search_mathlib", "lean_check"})
_runtime_id = runtime.IDENTITY
_lock = Lock()


class CheckpointWriteError(BaseException):
    pass


def runtime_id() -> str:
    return _runtime_id


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _file_fingerprint(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def manifest(*, run: dict, config, repo: Path) -> dict:
    """Bind a checkpoint to inputs whose changes can alter a resumed action."""
    settings = asdict(config)
    skill_paths = settings.pop("skills", [])
    role_roots = settings.pop("agent_dirs", [])
    settings.pop("status_context", None)  # the saved execution position owns this mutable context
    settings.pop("skills_root", None)
    settings["lea_root"] = str(settings.get("lea_root") or "")
    files: dict[str, str | None] = {}
    if repo.exists():
        for root, dirs, names in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in {".git", ".lake", "node_modules", "build"}]
            for name in names:
                path = Path(root) / name
                if path.suffix in {".lean", ".toml", ".json", ".lock"}:
                    files[str(path.relative_to(repo))] = _file_fingerprint(path)
    role_contents = []
    for root in role_roots or []:
        root_path = Path(root)
        if root_path.exists():
            role_contents.extend(_file_fingerprint(p) for p in sorted(root_path.rglob("*.yaml")))
            role_contents.extend(_file_fingerprint(p) for p in sorted(root_path.rglob("*.md")))
    from lea import agent, mcp, registry, tools
    tool_code = [_file_fingerprint(Path(module.__file__))
                 for module in (agent, tools, registry, mcp)]
    return {
        "schema": SCHEMA_VERSION,
        "session_id": run["session_id"],
        "target_id": run.get("focus_formalization_id"),
        "source_hash": run.get("focus_source_hash"),
        "model": run.get("model"),
        "config_digest": _digest(settings),
        "skill_digests": [_file_fingerprint(Path(p)) for p in skill_paths],
        "role_digests": sorted(role_contents),
        "tool_code_digests": tool_code,
        "files": files,
    }


def start_attempt(run_id: str) -> str:
    attempt_id = uuid4().hex
    now = db.utc_now()
    with db.write() as conn:
        row = conn.execute("select status from runs where id = ?", (run_id,)).fetchone()
        if not row or row["status"] != "running":
            raise RuntimeError("Run is no longer active")
        conn.execute("insert into run_attempts(id, run_id, runtime_id, started_at) values (?, ?, ?, ?)",
                     (attempt_id, run_id, _runtime_id, now))
        conn.execute("update runs set attempt_id = ? where id = ?", (attempt_id, run_id))
    return attempt_id


class Recorder:
    def __init__(self, run_id: str, attempt_id: str, manifest_value):
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.manifest = manifest_value

    def __call__(self, event: str, payload: dict) -> None:
        try:
            self._write(event, payload)
        except Exception as exc:
            raise CheckpointWriteError(f"Could not save {event}: {exc}") from exc

    def _write(self, event: str, payload: dict) -> None:
        now = db.utc_now()
        with db.write() as conn:
            owner = conn.execute("select attempt_id, status from runs where id = ?", (self.run_id,)).fetchone()
            if not owner or owner["attempt_id"] != self.attempt_id or owner["status"] != "running":
                raise RuntimeError("Obsolete run attempt cannot write a checkpoint")
            if event == "checkpoint":
                conn.execute("""insert into run_checkpoints(run_id, attempt_id, created_at,
                                schema_version, payload, manifest) values (?, ?, ?, ?, ?, ?)""",
                             (self.run_id, self.attempt_id, now, SCHEMA_VERSION,
                              _json(payload), _json(self.manifest() if callable(self.manifest) else self.manifest)))
                conn.execute("update runs set checkpoint_at = ? where id = ?", (now, self.run_id))
                conn.execute("""delete from run_checkpoints where run_id = ? and id not in
                                (select id from run_checkpoints where run_id = ? order by id desc limit 2)""",
                             (self.run_id, self.run_id))
            elif event == "invocation_start":
                conn.execute("""insert into run_invocations(id, run_id, attempt_id, kind, name,
                                turn, call_index, input_json, started_at)
                                values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                             (payload["id"], self.run_id, self.attempt_id, payload["kind"],
                              payload["name"], payload.get("turn", 0), payload.get("call_index"),
                              _json(payload.get("input")), now))
            elif event == "invocation_result":
                updated = conn.execute("""update run_invocations set result_json = ?, completed_at = ?
                                where id = ? and run_id = ? and attempt_id = ? and completed_at is null""",
                             (_json(payload.get("result")), now, payload["id"], self.run_id,
                              self.attempt_id))
                if updated.rowcount != 1:
                    raise RuntimeError("Invocation result has no matching pending action")
            else:
                raise ValueError(f"Unknown checkpoint event: {event}")


def finish_attempt(run_id: str, attempt_id: str, outcome: str) -> None:
    with db.write() as conn:
        conn.execute("update run_attempts set ended_at = ?, outcome = ? where id = ? and run_id = ?",
                     (db.utc_now(), outcome, attempt_id, run_id))
        if outcome not in {"pending", "running"}:
            conn.execute("""update runs set recovery_state = null, recovery_reason = null
                            where id = ? and attempt_id = ? and recovery_state = 'recovering'""",
                         (run_id, attempt_id))


def _record_transition(conn, run, code: str, message: str, severity: str = "degraded") -> None:
    payload = diagnostics.resolve(severity, code, message, source="recovery", context={
        "runtime_id": _runtime_id, "previous_attempt_id": run["attempt_id"],
        "recovery_count": run["recovery_count"],
    })
    conn.execute("""insert into timeline (session_id, run_id, kind, author, content, data, created_at)
                    values (?, ?, 'diagnostic', 'environment', ?, ?, ?)""",
                 (run["session_id"], run["id"], message, _json(payload), db.utc_now()))


def _pause(conn, run, reason: str) -> None:
    run_id = run["id"]
    conn.execute("""update runs set status = 'cancelled', recoverable = 1,
                    recovery_state = 'review', recovery_reason = ?,
                    result_kind = 'interrupted', result_detail = ?,
                    usage_status = case when usage_revision > 0 then 'partial' else 'unknown' end,
                    updated_at = ? where id = ? and status = 'running'""",
                 (reason, reason, db.utc_now(), run_id))
    _record_transition(conn, run, "run.recovery_review", reason)


def recover_running() -> dict[str, int]:
    """Make a restart decision for each former worker; never replay uncertain work."""
    resumed = paused = stopped = 0
    with db.write() as conn:
        runs = conn.execute("select * from runs where status = 'running' order by created_at, id").fetchall()
        for run in runs:
            rid = run["id"]
            if run["stop_requested_reason"]:
                reason = run["stop_requested_reason"]
                conn.execute("""update runs set status = 'cancelled', stop_reason = ?,
                               result_kind = 'cancelled', result_detail = ?, updated_at = ? where id = ?""",
                             (reason, f"Run stopped after adapter interruption ({reason}).", db.utc_now(), rid))
                _record_transition(conn, run, "run.recovery_stopped", f"The accepted {reason} request was preserved after the adapter stopped.")
                stopped += 1
                continue
            if run["deadline_at"]:
                try:
                    if datetime.fromisoformat(run["deadline_at"]) <= datetime.now(timezone.utc):
                        conn.execute("""update runs set status = 'cancelled', stop_reason = 'timeout',
                                       result_kind = 'cancelled', result_detail = ?,
                                       usage_status = case when usage_revision > 0 then 'partial' else 'unknown' end,
                                       updated_at = ? where id = ?""",
                                     ("The original run deadline expired while the adapter was unavailable.", db.utc_now(), rid))
                        _record_transition(conn, run, "run.recovery_stopped", "The original run deadline expired while the adapter was unavailable.")
                        stopped += 1
                        continue
                except ValueError:
                    _pause(conn, run, "The stored run deadline is invalid.")
                    paused += 1
                    continue
            checkpoint = conn.execute("""select * from run_checkpoints where run_id = ?
                                           order by id desc limit 1""", (rid,)).fetchone()
            if not checkpoint or checkpoint["schema_version"] != SCHEMA_VERSION:
                _pause(conn, run, "No compatible execution checkpoint is available.")
                paused += 1
                continue
            saved_position = json.loads(checkpoint["payload"])
            if (saved_position.get("max_cost_usd") is not None and
                    float(saved_position.get("cost_usd", 0)) >= float(saved_position["max_cost_usd"])):
                conn.execute("""update runs set status = 'cancelled', stop_reason = 'cost_cap',
                               result_kind = 'max_cost', result_detail = 'Original cost allowance was exhausted.',
                               updated_at = ? where id = ?""", (db.utc_now(), rid))
                _record_transition(conn, run, "run.recovery_stopped", "Original cost allowance was exhausted.")
                stopped += 1
                continue
            if run["max_turns"] is not None and saved_position.get("stage") == "turn_boundary" and int(saved_position.get("turn", 0)) >= int(run["max_turns"]):
                conn.execute("""update runs set status = 'max_turns', stop_reason = 'turn_cap',
                               result_kind = 'max_turns', result_detail = 'Original turn allowance was exhausted.',
                               updated_at = ? where id = ?""", (db.utc_now(), rid))
                _record_transition(conn, run, "run.recovery_stopped", "Original turn allowance was exhausted.")
                stopped += 1
                continue
            if run["recovery_count"] >= 1:
                _pause(conn, run, "Automatic recovery was already attempted for this run.")
                paused += 1
                continue
            # A concurrent child can start before a newer checkpoint written
            # by a different tool. Its unresolved action still blocks replay.
            pending = conn.execute("""select * from run_invocations where run_id = ?
                                        and completed_at is null order by started_at""",
                                   (rid,)).fetchall()
            unsafe = next((i for i in pending if i["kind"] != "tool" or i["name"] not in RETRY_SAFE_TOOLS), None)
            if unsafe:
                action = f"{unsafe['kind']} {unsafe['name']}"
                _pause(conn, run, f"Interrupted during {action}; review before retrying an uncertain action.")
                paused += 1
                continue
            # A completed action after the checkpoint can be replayed only if its
            # result is itself represented by a newer checkpoint. If not, pause.
            stage = saved_position.get("stage")
            later = conn.execute("""select kind, name, call_index from run_invocations
                                      where run_id = ? and completed_at > ?""",
                                 (rid, checkpoint["created_at"])).fetchall()
            if later and (stage != "model_complete" or any(
                i["kind"] != "tool" or i["call_index"] is None or i["name"] not in RETRY_SAFE_TOOLS
                for i in later
            )):
                _pause(conn, run, "An action completed after the last saved execution position.")
                paused += 1
                continue
            conn.execute("""update runs set status = 'pending', recovery_state = 'recovering',
                            recovery_reason = 'Resuming saved progress', recovery_count = recovery_count + 1,
                            updated_at = ? where id = ?""", (db.utc_now(), rid))
            _record_transition(conn, run, "run.recovering", "Resuming saved progress from a checked execution boundary.", "info")
            resumed += 1
    return {"resumed": resumed, "paused": paused, "stopped": stopped}


def load_checkpoint(run_id: str) -> tuple[dict, dict] | None:
    with db.connect() as conn:
        row = conn.execute("select payload, manifest from run_checkpoints where run_id = ? order by id desc limit 1",
                           (run_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        if payload.get("stage") == "model_complete":
            items = conn.execute("""select call_index, result_json from run_invocations where run_id = ?
                                   and completed_at is not null and call_index is not null
                                   and started_at >= (select created_at from run_checkpoints
                                   where run_id = ? order by id desc limit 1)""",
                                 (run_id, run_id)).fetchall()
            payload["completed_tools"] = {
                **payload.get("completed_tools", {}),
                **{str(item["call_index"]): json.loads(item["result_json"])
                   for item in items},
            }
    return payload, json.loads(row["manifest"])


def load_safe_conversation(run_id: str) -> list | None:
    """A manual successor starts a new turn after the last complete turn."""
    with db.connect() as conn:
        rows = conn.execute("select payload from run_checkpoints where run_id = ? order by id desc",
                            (run_id,)).fetchall()
    for row in rows:
        payload = json.loads(row["payload"])
        if payload.get("stage") == "turn_boundary":
            return payload.get("messages")
    return None


def pause_for_mismatch(run_id: str, reason: str) -> None:
    with db.write() as conn:
        run = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
        conn.execute("""update runs set status = 'cancelled', recoverable = 1,
                        recovery_state = 'review', recovery_reason = ?, result_kind = 'interrupted',
                        result_detail = ?, updated_at = ? where id = ? and status in ('pending', 'running')""",
                     (reason, reason, db.utc_now(), run_id))
        if run:
            _record_transition(conn, run, "run.recovery_review", reason)
