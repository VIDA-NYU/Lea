"""Durable execution boundaries and conservative crash recovery.

The prover calls ``record`` synchronously. A failed write is fatal to that
activation: no paid request or tool is allowed to follow an unrecorded intent.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from copy import deepcopy
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


def _valid_usage_details(details) -> bool:
    if details is None:
        return True  # supported checkpoints predate additive usage metadata
    if not isinstance(details, dict):
        return False
    for key in ("cached_input_tokens", "cache_write_tokens", "reasoning_tokens",
                "unpriced_calls", "details_missing_calls"):
        value = details.get(key)
        if value is not None and (type(value) is not int or value < 0):
            return False
    counts = details.get("basis_counts", {})
    return (isinstance(counts, dict) and all(isinstance(key, str) and
            type(value) is int and value >= 0 for key, value in counts.items()))


def _file_fingerprint(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _dependency_fingerprints(repo: Path, *, snapshot=None) -> dict:
    """Record the enclosing Lake configuration and installed package revisions."""
    root = next((p for p in [repo, *repo.parents]
                 if (p / "lakefile.lean").exists() or (p / "lakefile.toml").exists()), None)
    if root is None:
        return {}
    observe=(lambda path:snapshot.observation(path)[0]) if snapshot else _file_fingerprint
    if snapshot:
        from lea.check_evidence import file_version
        for candidate in (root/'.lake/packages',root/'lake-packages',root/'proofs'):
            snapshot.topology[str(candidate)]=file_version(candidate)
    values = {name: observe(root / name) for name in
              ("lakefile.lean", "lakefile.toml", "lake-manifest.json", "lean-toolchain")}
    for packages in (root / ".lake/packages", root / "lake-packages"):
        if not packages.is_dir():
            continue
        for package in sorted(packages.iterdir()):
            gitdir = package / ".git"
            if snapshot:snapshot.topology[str(gitdir)]=file_version(gitdir)
            if gitdir.is_file():
                if snapshot:snapshot.observation(gitdir)
                entry = gitdir.read_text().strip()
                if entry.startswith("gitdir: "):
                    gitdir = (package / entry[8:]).resolve()
            head = gitdir / "HEAD"
            values[f"{package.name}:HEAD"] = observe(head)
            if head.exists():
                ref = head.read_text().strip()
                if ref.startswith("ref: "):
                    values[f"{package.name}:revision"] = observe(gitdir / ref[5:])
                    values[f"{package.name}:packed-refs"] = observe(gitdir / "packed-refs")
    # Sibling proof modules share this Lake project and may be imported by the
    # target. Their content is outside the target's own Git repository.
    proofs = root / "proofs"
    if proofs.is_dir():
        if snapshot:
            for directory in proofs.rglob('*'):
                if directory.is_dir():snapshot.topology[str(directory)]=file_version(directory)
        for path in sorted(proofs.rglob("*.lean")):
            if not path.is_relative_to(repo):
                values[str(path.relative_to(root))] = observe(path)
    return values


def manifest(*, run: dict, config, repo: Path) -> dict:
    """Bind a checkpoint to inputs whose changes can alter a resumed action."""
    settings = asdict(config)
    skill_paths = settings.pop("skills", [])
    role_roots = settings.pop("agent_dirs", [])
    settings.pop("status_context", None)  # the saved execution position owns this mutable context
    settings.pop("project_context", None)  # fresh reservations belong to the current execution segment
    settings.pop("skills_root", None)
    if not settings.get('selected_result'):
        settings.pop('selected_result', None)  # canonical checkpoints retain their existing config contract
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
    from lea import agent, mcp, registry, tools, project_library_tools, providers, condenser, events, stall, usage, subagents, check_evidence, lean_checks, check_runtime, interface, lsp_daemon, imports, execution, result_selection, safeverify
    tool_code = [_file_fingerprint(Path(module.__file__))
                 for module in (agent, tools, registry, mcp, project_library_tools,
                                providers, condenser, events, stall, usage, subagents, check_evidence, lean_checks, check_runtime, interface, lsp_daemon, imports, execution, result_selection, safeverify)]
    tool_code.extend(_file_fingerprint(path) for path in (result_selection.INSPECTOR,
        safeverify.SAFE_VERIFY_DIR / 'Main.lean', safeverify.SAFE_VERIFY_DIR / 'SafeVerify/Monads.lean'))
    tool_modules = {}
    for name in config.tool_modules:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        tool_modules[name] = _file_fingerprint(Path(spec.origin)) if spec and spec.origin else None
    # Allowances and source revisions can change on an explicit continuation;
    # executable configuration must still match the saved agent.
    execution_settings = {k: v for k, v in settings.items() if k not in {
        "max_turns", "max_cost_usd", "max_spend_usd", "allow_source_pause",
    }}
    session = store.get_session(run["session_id"]) or {}
    project = store.get_project(session["project_id"]) if session.get("project_id") else None
    prompt = agent.load_system_prompt(config.prompt_variant, config.skills, workspace=str(repo),
                                     namespace=project.get("namespace") if project else None)
    # Per-run materialization paths change even when skill contents do not.
    if config.skills_root:
        prompt = prompt.replace(str(config.skills_root), "<skills-root>")
    skill_resources = {}
    if config.skills_root:
        root = Path(config.skills_root)
        skill_resources = {str(p.relative_to(root)): _file_fingerprint(p)
                           for p in sorted(root.rglob("*")) if p.is_file()}
    return {
        "schema": SCHEMA_VERSION,
        "session_id": run["session_id"],
        "target_id": run.get("focus_formalization_id"),
        "source_hash": run.get("focus_source_hash"),
        "model": run.get("model"),
        "config_digest": _digest(settings),
        "execution_digest": _digest(execution_settings),
        "prompt_digest": _digest(prompt),
        "skill_digests": [_file_fingerprint(Path(p)) for p in skill_paths],
        "skill_resource_digests": skill_resources,
        "role_digests": sorted(role_contents),
        "tool_code_digests": tool_code,
        "tool_module_digests": tool_modules,
        "files": files,
        "dependencies": _dependency_fingerprints(repo),
    }


def continuation_checkpoint(run_id: str, checkpoint_id: int | None = None) -> tuple[dict, dict, int]:
    """Ordinary pauses resume only a complete, durably journaled turn."""
    with db.connect() as conn:
        row = conn.execute("""select * from run_checkpoints where run_id = ?
                              and (? is null or id = ?) order by id desc limit 1""",
                           (run_id, checkpoint_id, checkpoint_id)).fetchone()
        if not row or row["schema_version"] != SCHEMA_VERSION:
            raise ValueError("Saved agent state is unavailable. Use Re-formalize to start a new attempt.")
        try:
            state = json.loads(row["payload"])
            saved_manifest = json.loads(row["manifest"])
        except (ValueError, TypeError) as exc:
            raise ValueError("Saved agent state is unreadable. Use Re-formalize to start a new attempt.") from exc
        if (not isinstance(state, dict) or not isinstance(saved_manifest, dict)
                or not isinstance(saved_manifest.get("files", {}), dict)
                or not isinstance(state.get("status_context", {}), dict)
                or not isinstance(state.get("proof_state", {}), dict)):
            raise ValueError("Saved checkpoint format is incompatible. Use Re-formalize to start a new attempt.")
        detector = state.get("stall_detector", {})
        if (not isinstance(detector, dict)
                or type(detector.get("failures", 0)) is not int or detector.get("failures", 0) < 0
                or type(detector.get("evidence_revision", 0)) is not int or detector.get("evidence_revision", 0) < 0
                or detector.get("fingerprint") is not None and
                   (not isinstance(detector["fingerprint"], str) or len(detector["fingerprint"]) != 64)
                or not isinstance(detector.get("seen_evidence", []), list)
                or any(not isinstance(x, str) or len(x) != 64 for x in detector.get("seen_evidence", []))
                or not _valid_usage_details(state.get("usage_details"))
                or not isinstance(state.get("dependency_evidence", {}), dict)
                or any(type(state.get(k, 0)) is not int or state.get(k, 0) < 0 for k in ("input_tokens", "output_tokens"))
                or type(state.get("cost_usd", 0)) not in (int, float)
                or not math.isfinite(state.get("cost_usd", 0)) or state.get("cost_usd", 0) < 0):
            raise ValueError("Saved checkpoint accounting or stall state is corrupt. Review interrupted work before continuing.")
        if (state.get("stage") != "turn_boundary" or not isinstance(state.get("messages"), list)
                or type(state.get("turn")) is not int or state["turn"] < 0
                or any(not isinstance(m, dict) for m in state["messages"])):
            raise ValueError("The saved execution point is not a complete turn. Review interrupted work before continuing.")
        uncertain = conn.execute("""select id from run_invocations where run_id = ?
            and (completed_at is null or (completed_at > ? and not
                 (kind='model_auxiliary' and name='manual_compaction'))) limit 1""", (run_id, row["created_at"])).fetchone()
        if uncertain:
            raise ValueError("An action may have completed outside the saved state. Review interrupted work before continuing.")
    return state, saved_manifest, row["id"]


def compatible_runtime(old: dict, current: dict, state: dict) -> bool:
    """Allow only pinned predecessor/destination pairs at a settled boundary.

    Tool/model/skill/config checks remain independent. The allowlist names the
    destination too, so a later arbitrary runtime cannot inherit this exception.
    """
    if old.get("tool_code_digests") == current.get("tool_code_digests"):
        return True
    if (state.get("stage") != "turn_boundary" or not state.get("tool_schema_digest")
            or not old.get("execution_digest") or not old.get("prompt_digest")):
        return False
    try:
        releases = json.loads(Path(__file__).with_name("checkpoint_compatibility.json").read_text())
    except (OSError, ValueError):
        return False
    return any(old.get("tool_code_digests") == release["predecessor"]
               and current.get("tool_code_digests") == release["destination"] for release in releases)


def compatible_crash_manifest(old: dict, current: dict, state: dict) -> bool:
    # Crash recovery does not rebase source/dependencies/files or reset progress.
    return (compatible_runtime(old, current, state)
            and {k: v for k, v in old.items() if k != "tool_code_digests"}
            == {k: v for k, v in current.items() if k != "tool_code_digests"})


def prepare_continuation(run: dict, config, repo: Path, status_context: dict, task: str) -> tuple[dict, bool]:
    """Rebase a pinned execution snapshot into a new accounting segment."""
    saved, old_manifest, _ = continuation_checkpoint(run["resumes_run_id"], run["resume_checkpoint_id"])
    current = manifest(run=run, config=config, repo=repo)
    keys = ("schema", "session_id", "target_id", "model", "skill_digests", "role_digests")
    compatible = (compatible_runtime(old_manifest, current, saved)
                  and all(old_manifest.get(k) == current.get(k) for k in keys))
    if "execution_digest" in old_manifest:
        compatible = compatible and all(old_manifest.get(k) == current.get(k) for k in ("execution_digest", "prompt_digest"))
        compatible = compatible and old_manifest.get("skill_resource_digests", {}) == current["skill_resource_digests"]
        compatible = compatible and old_manifest.get("tool_module_digests", {}) == current["tool_module_digests"]
    else:
        # Legacy snapshots can be resumed only when their original configuration
        # is still reproducible. Never treat an absent digest as a match.
        from dataclasses import replace
        original = replace(config, max_turns=saved.get("max_turns"), max_cost_usd=saved.get("max_cost_usd"))
        compatible = compatible and old_manifest.get("config_digest") == manifest(run=run, config=original, repo=repo)["config_digest"]
    if config.mcp_servers and not saved.get("tool_schema_digest"):
        compatible = False
    if not compatible:
        raise ValueError("Saved agent configuration is incompatible with the current runtime. Use Re-formalize to start a new attempt.")
    state = deepcopy(saved)
    changed_files = sorted(k for k in old_manifest.get("files", {}).keys() | current["files"].keys()
                           if old_manifest.get("files", {}).get(k) != current["files"].get(k))
    source_changed = old_manifest.get("source_hash") != current.get("source_hash")
    previous_source = saved.get("status_context", {}).get("source_bundle", {})
    new_source = status_context.get("source_bundle", {})
    source_changes = {k: {"before": previous_source.get(k), "after": new_source.get(k)}
                      for k in ("statement", "proof", "uses", "context", "proofAssociation", "relevantSource")
                      if previous_source.get(k) != new_source.get(k)}
    dependencies_changed = old_manifest.get("dependencies", {}) != current["dependencies"]
    changed = source_changed or bool(changed_files) or bool(source_changes) or dependencies_changed
    state.update(input_tokens=0, output_tokens=0, cost_usd=0.0, usage_details=None,
                 stall_detector={}, reporting_stop=None,
                 max_turns=config.max_turns, max_cost_usd=config.max_cost_usd,
                 status_context=deepcopy(status_context))
    state["status_context"]["instruction"] = status_context.get("instruction", "") + (
        " This continuation has explicit best-effort authorization for disclosed conventional inferred context; "
        "explicit source text remains authoritative."
        if run.get("continuation_best_effort") else
        " Continue faithfully. Best-effort permission from earlier segments does not apply to this continuation.")
    if not source_changed and saved.get("status_context", {}).get("assessment"):
        state["status_context"]["assessment"] = saved["status_context"]["assessment"]
    if changed and state.get("proof_state"):
        state["proof_state"].update(unchecked_write=True, latest_check_passed=None)
    state["messages"] = [m for m in state["messages"] if not m.get("lea_status_context")]
    if run.get("project_id"):
        from . import projects
        project = store.get_project(run["project_id"])
        if project:
            fresh = projects.compose_context_message(project, repo)
            state["messages"] = [m for m in state["messages"] if not projects.is_context_message(m)]
            if fresh:
                state["messages"].insert(0, fresh)
    state["messages"].append({"role": "user", "lea_continuation_context": True,
        "mathematical_request": status_context.get("source_bundle"),
        "content": task + "\n\nSource revision changed: " + str(source_changed)
            + ". Changed project files: " + json.dumps(changed_files)
            + ". Dependency revision changed: " + str(dependencies_changed)
            + ". Source changes: " + json.dumps(source_changes, ensure_ascii=False)
            + ". Reassess affected work against the current source; earlier findings remain prior evidence."})
    return state, changed


def available_action(run: dict) -> str | None:
    """Derived UI action; eligibility is checked again by the mutation route."""
    if run.get("recovery_state") == "review" and run.get("recoverable"):
        return "resume"
    if (run.get("purpose") != "overleaf_solver" or not run.get("recoverable")
            or run.get("status") not in {"cancelled", "max_turns"}
            or run.get("stop_reason") == "superseded"):
        return None
    with db.connect() as conn:
        if conn.execute("select 1 from runs where resumes_run_id = ? or (focus_formalization_id = ? and id != ? and created_at >= ?)",
                        (run["id"], run.get("focus_formalization_id"), run["id"], run["created_at"])).fetchone():
            return None
        if conn.execute("select 1 from runs where session_id = ? and status in ('pending', 'running')", (run["session_id"],)).fetchone():
            return None
    try:
        continuation_checkpoint(run["id"])
    except (ValueError, LookupError):
        return None
    return "continue"


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
    def __init__(self, run_id: str, attempt_id: str, manifest_value, *, command_token=None):
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.manifest = manifest_value
        self.command_token = command_token

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
                if self.command_token:
                    from .run_commands import authorize_invocation
                    authorize_invocation(conn,self.run_id,self.command_token,payload)
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
