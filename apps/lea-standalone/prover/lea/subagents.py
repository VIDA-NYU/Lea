"""Subagents — `spawn_subagent` + child activations (v2.3 item 18).

A parent (coordinator) tool call starts a **child activation**: a fresh, stateless
`run_events` with its own message list, its own working directory, and a fresh
context. The child explores in isolation and returns a *distilled result* — a
candidate file + its `lean_check` verdict + a short summary — which the parent
collates. The child never writes the canonical proof file (D76): its `working_dir`
IS an ignored per-run candidate dir, so F3's write sandbox physically confines it
there.

**Two independent recursion guards** (belt and suspenders — a bug in one is not a
fork bomb):

  1. *Depth walk.* Each activation runs at a `runctx` depth; a top-level run is 0, a
     child is parent+1. `spawn_subagent` refuses at ``depth >= max_depth`` (default
     **1**), so a child (depth 1) can never spawn.
  2. *Toolset exclusion.* `spawn_subagent` is registered ``opt_in=True``, so it is
     absent from any ``tools=None`` toolset — and a child is built with ``tools=None``.
     A child cannot even *see* the tool.

Either guard alone suffices. The full typed result envelope + transcript storage is
item 22; item 18 returns a compact human/LLM-readable block.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import dataclasses
import queue as _queue
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import diagnostics
from . import lsp_daemon
from .errors import ToolError
from .events import CheckResult, Finished, SubagentFinished, SubagentProgress
from .profiles import AgentProfile, available_profiles, load_profile
from .registry import build_toolset, get_tool, tool
from .runctx import (
    current_config,
    current_depth,
    current_run_key,
    current_should_stop,
    current_working_dir,
)

# Default nesting cap: one level of children. A depth-0 coordinator may spawn; a
# depth-1 subagent may not. Kept here (not a config knob yet) so item 18 pins the
# safe default; a future item can thread it through config with a design pass.
DEFAULT_MAX_DEPTH = 1

# E2: how many children may run at once. A hard cap on concurrent `lean --worker`
# processes (each loads Mathlib into RAM) so a model that spawns many at once can't
# drive the box into swap — the same laptop-safety concern B1/B3 guard. Children beyond
# the cap queue and start as slots free.
DEFAULT_MAX_CONCURRENT_CHILDREN = 5

# A runaway guard on a child that never converges — NOT accounting. A child with no
# turn budget of its own (parent unlimited) still can't loop forever.
DEFAULT_CHILD_MAX_TURNS = 15

_SPAWN_SCHEMA = {
    "description": (
        "Delegate a self-contained subtask to a fresh subagent that works in an "
        "isolated scratch directory and returns a distilled result (a candidate "
        "file, its lean_check verdict, and a short summary). Use this to explore a "
        "proof strategy, search Mathlib, or try a candidate WITHOUT polluting your "
        "own context with the dead ends. The subagent cannot write your canonical "
        "proof file and cannot spawn further subagents — you collate its result and "
        "commit the final artifact yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "A short (3-5 word) description of the subtask.",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "The full task for the subagent, self-contained: it starts with a "
                    "fresh context and sees only this prompt, not your conversation. "
                    "State the GOAL — the exact lemma/statement to prove or the specific "
                    "thing to find — NOT a file path to write. The subagent writes its "
                    "candidate in its own isolated scratch directory; you collate the "
                    "result and commit the canonical file yourself. Do not tell it where "
                    "to write (any path you give is ignored / redirected into its scratch)."
                ),
            },
            # Filled in per run by `build_spawn_schema` (B1) — see below.
            "subagent_type": {
                "type": "string",
                "description": (
                    "Which role to run, or omit for a generalist with the full scoped "
                    "toolset. An unknown role is refused."
                ),
            },
        },
        "required": ["description", "prompt"],
    },
}


GENERALIST = "generalist"


def build_spawn_schema() -> dict:
    """The `spawn_subagent` schema for THIS run, with the roles that actually exist (B1).

    The schema used to be frozen at import with `subagent_type` as free text and the role
    names mentioned only in English prose. That survives two hardcoded roles and breaks
    the moment a role is added: the model has no way to learn it exists, so it is never
    delegated to — **and nothing errors**. The user configures a sub-agent and simply
    nothing happens, which is the worst failure shape available, because there is no
    symptom to search for.

    So the roles are discovered per build and published two ways, because they do
    different jobs:
      * ``enum`` — machine-checkable. The model cannot invent a role, and a provider that
        validates arguments rejects a typo before it reaches us.
      * the ``description`` — how the model *chooses*. An enum of bare names says nothing
        about when to use one, so each role's own `description` is listed beside it. This
        is where a user-authored role's "when to use this" ends up doing real work.
    """
    roles = []
    for name in available_profiles():
        try:
            roles.append((name, (load_profile(name).description or "").strip()))
        except ToolError:
            # A malformed role must not take the whole toolset down with it; it simply
            # isn't offered. `load_profile` raises again, with detail, if it is requested.
            continue

    lines = [
        f"- {GENERALIST}: a fresh agent with your own toolset, no specialization.",
    ] + [f"- {name}: {desc}" if desc else f"- {name}" for name, desc in roles]

    schema = json.loads(json.dumps(_SPAWN_SCHEMA))   # deep copy; never mutate the template
    schema["input_schema"]["properties"]["subagent_type"] = {
        "type": "string",
        "enum": [GENERALIST] + [name for name, _ in roles],
        "description": "Which role to run. Omit for the generalist.\n" + "\n".join(lines),
    }
    return {
        "name": "spawn_subagent",
        "description": _SPAWN_SCHEMA["description"],
        "input_schema": schema["input_schema"],
    }


def _bounded_child_turns(parent_max_turns: int | None) -> int:
    """A child's turn cap: the parent's, but never unbounded and never above the
    runaway ceiling."""
    if parent_max_turns is None:
        return DEFAULT_CHILD_MAX_TURNS
    return max(1, min(parent_max_turns, DEFAULT_CHILD_MAX_TURNS))


def _parent_tool_names(parent_config) -> set[str]:
    """The tools the parent activation can actually call — the ceiling a child can
    never exceed (item 21). Resolved through the same ``build_toolset`` the loop uses,
    so it is exactly the parent's real capability set, opt-in tools included when the
    parent explicitly selected them (a coordinator's ``spawn_subagent``)."""
    schemas, _ = build_toolset(parent_config.tools, getattr(parent_config, 'extra_tools', None))
    return {s["name"] for s in schemas}


def _child_available_names(parent_config) -> set[str]:
    """The tools that will actually EXIST in a child activation.

    A child is built with ``mcp_servers={}`` (children stay light — each would otherwise
    spawn another Lean server against the same Mathlib), so MCP tools are present in the
    PARENT's registry and absent from the child's. Composing against the parent alone
    therefore let MCP names through, and the child then died in ``build_toolset`` with
    "unknown tool 'lean_build'" — a hard failure at spawn, for a name that was legitimate
    where it was checked.

    This hit the GENERALIST path too, not just imported roles: a generalist's wanted-set is
    the parent's whole live toolset, MCP tools included.

    A child does inherit ``http_tools`` (they cost nothing to re-register), so those stay.
    """
    from .registry import REGISTRY

    names = {n for n, t in REGISTRY.items() if not t.opt_in}
    names |= {str(spec.get("name")) for spec in (getattr(parent_config, "http_tools", None) or [])}
    return names


def compose_child_tools(parent_config, declared: list[str] | None) -> list[str]:
    """A child's effective toolset = its declared toolset INTERSECTED with the parent's
    (item 21, D79). Permissions compose by **tightening only**: a child can never call
    a tool the parent lacked, so spawning is not a privilege-escalation path.

    ``declared is None`` (the generalist) means the default toolset; either way every
    opt-in tool (``spawn_subagent``) is stripped, so a child can never spawn — the same
    guarantee as the depth walk, now also at the capability layer. The intersection is
    with what the CHILD will have, not merely what the parent has — see
    ``_child_available_names``. Declared order is
    preserved (``build_toolset`` treats the list as filter+order). An unknown tool name
    is a profile typo and raises; a *known* tool the parent lacks is silently tightened
    away — that is the D79 contract, not an error.
    """
    parent_allowed = _parent_tool_names(parent_config) & _child_available_names(parent_config)
    wanted = [s["name"] for s in build_toolset(None)[0]] if declared is None else list(declared)
    effective: list[str] = []
    dropped: list[str] = []
    for name in wanted:
        t = get_tool(name)
        if t is None:
            # B4: SOFT-DROP, not raise. This used to abort the spawn, which meant deleting
            # a custom tool (or disabling the MCP server that provided it) silently broke
            # every role that named it — a confusing failure far from its cause. Blocking
            # the delete instead was the alternative, but that couples the Library to role
            # internals and leaves undeletable rows. So the child runs without it and the
            # human is told, which is the same policy a session's deleted override gets.
            dropped.append(name)
            continue
        if t.opt_in:
            continue  # opt-in (spawn_subagent) is never granted to a child
        if name in parent_allowed:  # else: parent lacked it → tightened away (D79)
            effective.append(name)
    if dropped:
        diagnostics.report(
            "degraded",
            "subagent.tool_dropped",
            f"A sub-agent asked for {', '.join(repr(d) for d in dropped)}, which no longer "
            f"exists. It ran without it.",
            source="subagent",
            remedy="The tool was probably deleted, or its MCP server is turned off. Edit "
                   "the role under Library → Sub-agents, or restore the tool.",
            once=True,
            tools=dropped,
        )
    return effective


def _child_config(parent_config, profile: AgentProfile | None):
    """Build the child's :class:`LeaConfig` from the parent + an optional role profile.
    A profile supplies model / scoped tools / turn cap / prompt head (item 19); each
    ``None`` field inherits the parent's. The toolset is always composed against the
    parent's (item 21) so a child ⊆ parent — no profile → the generalist default,
    tightened to the parent. MCP is dropped (children stay light) and turns bounded."""
    if profile is None:
        return dataclasses.replace(
            parent_config,
            tools=compose_child_tools(parent_config, None),
            mcp_servers={},
            max_turns=_bounded_child_turns(parent_config.max_turns),
            system_prompt_head=None,
            max_cost_usd=None,
        )
    # D6: merge the user's per-role override (from the Sub-agents page) OVER the role's
    # YAML defaults — model / max_turns / max_cost / system_prompt / tools. The override
    # retunes THIS spawn only; the vendored profile is never mutated. Tools still compose
    # against the parent (item 21), so an override can only TIGHTEN, never escalate.
    try:
        override = dict((parent_config.subagent_overrides or {}).get(profile.name) or {})
    except Exception:  # noqa: BLE001 — a malformed override must not crash the spawn path
        override = {}

    # An explicit cap (override or profile) is honored as declared; only an
    # inherited/unlimited cap is clamped to the runaway ceiling.
    if override.get("max_turns") is not None:
        max_turns = override["max_turns"]
    elif profile.max_turns is not None:
        max_turns = profile.max_turns
    else:
        max_turns = _bounded_child_turns(parent_config.max_turns)

    declared_tools = override["tools"] if override.get("tools") else profile.tools
    return dataclasses.replace(
        parent_config,
        model=override.get("model") or profile.model or parent_config.model,
        tools=compose_child_tools(parent_config, declared_tools),
        mcp_servers={},
        max_turns=max_turns,
        system_prompt_head=override.get("system_prompt") or profile.system_prompt,
        max_cost_usd=override.get("max_cost"),
    )


def _candidate_dir_for(base_working_dir: str | None, run_key: str, agent_id: str) -> Path:
    """The isolated scratch tree the child writes to: ``<wd>/.lea/tmp/<run>/<agent>``.
    Rooted under the parent's working dir when there is one (so the parent can read
    the candidate back to collate); otherwise a process-temp fallback."""
    import tempfile

    base = Path(base_working_dir) if base_working_dir else Path(tempfile.gettempdir())
    return base / ".lea" / "tmp" / run_key / agent_id


def _relativize(path: str, base: str | None) -> str:
    """A candidate path shown relative to the parent's working dir when it sits
    inside it, else the absolute path (so the parent can still `read_file` it)."""
    if not base:
        return path
    try:
        return str(Path(path).resolve().relative_to(Path(base).resolve()))
    except ValueError:
        return path


def _run_child(child_config, child_messages, *, working_dir: str, run_key: str,
               depth: int, should_stop=None):
    """Drive a child `run_events`, YIELDING each of its events (E1) while capturing the
    two the parent needs. A generator: it yields the child's raw `AgentEvent`s (the
    caller wraps them as `SubagentProgress` for the UI) and, via ``return``, hands back
    ``(last_check, finished)`` — the final `CheckResult` seen (or None) and the terminal
    `Finished` (or None if the child errored out). ``should_stop`` is the child's
    cooperative-stop predicate (D2): a per-child flag composed with the coordinator's.

    Item 18 still holds: the coordinator loop yields these up for the UI but never puts
    them in the model's `messages` — only the distilled result becomes the tool_result."""
    # Lazy import breaks the tools -> subagents -> agent import cycle: agent.py imports
    # tools (which imports this module), so this module cannot import agent at load.
    from .agent import run_events

    last_check: CheckResult | None = None
    finished: Finished | None = None
    for ev in run_events(
        child_config,
        child_messages,
        session_id=run_key,
        working_dir=working_dir,
        depth=depth,
        should_stop=should_stop,
        gate=None,            # a child never pauses for human approval
    ):
        if isinstance(ev, CheckResult):
            last_check = ev
        elif isinstance(ev, Finished):
            finished = ev
        yield ev
    return last_check, finished


@dataclass(frozen=True)
class SubagentResult:
    """The TYPED result of one child run (item 22). `render()` is the prose the parent
    model reads in its tool_result; `to_event()` is the structured `SubagentFinished`
    the adapter persists. The `transcript` is the child's full message list — kept here
    so it can be stored SEPARATELY (never as a code_step), and `result_id` is the audit
    handle that links a promoted candidate back to the run that produced it."""

    result_id: str
    subagent_type: str
    depth: int
    candidate_path: str | None      # relative to the parent's working dir when possible
    check_status: str | None        # 'ok' | 'error' | None (nothing checked)
    check_detail: str | None
    stop_reason: str                # child's Finished.reason, or 'error'
    summary: str                    # child's final text
    transcript: list = field(default_factory=list, repr=False)

    def render(self) -> str:
        """Compact, explicit prose for the parent — evidence, not a verdict of record;
        the parent re-checks any candidate it promotes."""
        lines = [f"[subagent:{self.subagent_type} depth={self.depth} id={self.result_id}] "
                 f"finished ({self.stop_reason})."]
        if self.check_status is not None:
            verdict = self.check_status
            if self.check_status == "error" and self.check_detail:
                verdict = f"error — {self.check_detail}"
            lines.append(f"candidate: {self.candidate_path}")
            lines.append(f"lean_check: {verdict}")
            lines.append("(read the candidate file to promote it; re-check after you write it.)")
        else:
            lines.append("candidate: (none written / checked)")
        if self.summary:
            lines.append(f"result: {self.summary}")
        return "\n".join(lines)

    def to_event(self) -> SubagentFinished:
        return SubagentFinished(
            result_id=self.result_id,
            subagent_type=self.subagent_type,
            candidate_path=self.candidate_path,
            check_status=self.check_status,
            check_detail=self.check_detail,
            stop_reason=self.stop_reason,
            summary=self.summary,
            transcript=self.transcript,
        )


def _build_result(
    result_id: str,
    subagent_type: str,
    depth: int,
    last_check: CheckResult | None,
    finished: Finished | None,
    base_working_dir: str | None,
) -> SubagentResult:
    """Distill a child run into the typed envelope."""
    return SubagentResult(
        result_id=result_id,
        subagent_type=subagent_type,
        depth=depth,
        candidate_path=_relativize(last_check.path, base_working_dir) if last_check else None,
        check_status=last_check.status if last_check else None,
        check_detail=last_check.detail if last_check else None,
        stop_reason=finished.reason if finished else "error",
        summary=finished.text if finished else "",
        transcript=list(finished.transcript.get("messages", [])) if finished else [],
    )


def _error_result(plan: "SpawnPlan", detail: str) -> SubagentResult:
    """The typed result for a child that RAISED before finishing (D1). Recorded so a
    started child always gets a `SubagentFinished` — otherwise the adapter's running row
    would never resolve. No candidate, no transcript; the error is the summary."""
    return SubagentResult(
        result_id=plan.result_id,
        subagent_type=plan.subagent_type,
        depth=plan.child_depth,
        candidate_path=None,
        check_status=None,
        check_detail=None,
        stop_reason="error",
        summary=f"error — {detail}",
        transcript=[],
    )


# --- per-activation result collector -------------------------------------------
# spawn_subagent records each child's typed result here; the agent loop drains it
# right after the tool call and yields a SubagentFinished event (mirroring how a
# lean_check call yields CheckResult). Scoped per activation so results can't leak
# across runs; a child activation opens its own (empty) scope, so recording after
# _run_child returns lands in the PARENT's collector, where the parent loop drains it.
_results: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "lea_subagent_results", default=None
)


def begin_results_scope():
    """Open a fresh collector for this activation; returns a reset token."""
    return _results.set([])


def end_results_scope(token) -> None:
    with contextlib.suppress(ValueError):
        _results.reset(token)


def _record_result(result: SubagentResult) -> None:
    collector = _results.get()
    if collector is not None:
        collector.append(result)


def drain_results() -> list[SubagentResult]:
    """Return and clear the results recorded since the last drain (the parent loop
    calls this right after a spawn_subagent tool call)."""
    collector = _results.get()
    if not collector:
        return []
    out = list(collector)
    collector.clear()
    return out


@dataclass(frozen=True)
class SpawnPlan:
    """Everything resolved for a spawn BEFORE the child runs (D1). `prepare_spawn`
    builds it (validate → role → id → scratch dir → child config); `run_prepared`
    consumes it. Splitting the two lets the coordinator loop emit `SubagentStarted`
    between them, so a running child is visible instead of appearing only on finish."""
    result_id: str
    subagent_type: str
    child_depth: int
    description: str          # the task-title line, for the running row's label
    child_config: object
    child_messages: list
    candidate_dir: Path
    parent_wd: str | None


def prepare_spawn(args: dict) -> "SpawnPlan | str":
    """Resolve a spawn WITHOUT running the child. Returns a :class:`SpawnPlan` on
    success, or an error string (the `tool_result` the model reads) when the call is
    refused — missing prompt, depth-cap breach, no live activation, or an unknown role.
    On an error string the coordinator loop emits NO `SubagentStarted` (nothing runs),
    so a refused spawn never leaves a phantom running child."""
    description = (args.get("description") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    subagent_type = (args.get("subagent_type") or GENERALIST).strip() or GENERALIST
    if not prompt:
        return "Error: spawn_subagent requires a non-empty 'prompt'."

    depth = current_depth()
    if depth >= DEFAULT_MAX_DEPTH:
        return (
            f"Error: a subagent (depth {depth}) cannot spawn further subagents "
            f"(max depth {DEFAULT_MAX_DEPTH}). Do this subtask directly."
        )

    parent_config = current_config()
    if parent_config is None:
        # spawn_subagent only makes sense inside a live activation (which sets the
        # config in the run context). A bare/mis-wired call gets a clear refusal, not
        # a crash.
        return "Error: spawn_subagent can only run inside an activation."

    # Resolve the role (item 19): 'generalist' (the default) keeps item 18's built-in
    # behavior; any other type must name a profile in lea/agents/ or is refused —
    # never silently downgraded to a full-toolset generalist.
    profile: AgentProfile | None = None
    if subagent_type != GENERALIST:
        try:
            profile = load_profile(subagent_type)
        except ToolError as exc:
            return f"Error: {exc}"

    parent_wd = current_working_dir()
    run_key = current_run_key() or uuid.uuid4().hex[:12]
    agent_id = f"{subagent_type}-{uuid.uuid4().hex[:8]}"
    candidate_dir = _candidate_dir_for(parent_wd, run_key, agent_id)
    candidate_dir.mkdir(parents=True, exist_ok=True)

    # D2: register a per-child stop flag now, keyed by the child's id, so an interrupt
    # endpoint can find and set it the instant SubagentStarted surfaces the child —
    # even though the child itself only starts running in run_prepared_events. Cleared
    # there in a finally.
    _child_stops[agent_id] = threading.Event()

    child_config = _child_config(parent_config, profile)
    task = f"{description}\n\n{prompt}" if description else prompt
    child_messages = [{"role": "user", "content": task}]
    # The running row's label: the same first line `_subagent_child_title` would pick
    # from the child's first user message (bridge.py), but available now — before the
    # child has produced any transcript.
    title_hint = description or (task.splitlines()[0].strip() if task else subagent_type)

    return SpawnPlan(
        result_id=agent_id,
        subagent_type=subagent_type,
        child_depth=depth + 1,
        description=title_hint,
        child_config=child_config,
        child_messages=child_messages,
        candidate_dir=candidate_dir,
        parent_wd=parent_wd,
    )


# --- per-child cooperative stop (D2) -------------------------------------------
# Keyed by the child's result_id. prepare_spawn registers an Event; run_prepared_events
# clears it in a finally. `request_child_stop` (called in-process by the adapter's
# interrupt endpoint) sets it; the child's should_stop polls it — so a user can kill one
# runaway child without cancelling the whole coordinator run. Process-global (not a
# ContextVar): the endpoint runs on a DIFFERENT thread from the child's, and a
# threading.Event is the thread-safe hand-off between them.
_child_stops: dict[str, threading.Event] = {}


def request_child_stop(result_id: str) -> bool:
    """Ask a single running child to stop cleanly (D2). Returns True if a live child by
    that id was found and flagged, False if it is unknown/already gone. The child returns
    its partial findings at its next turn boundary (the same clean stop as a coordinator
    interrupt), so the coordinator still gets a usable `SubagentFinished`."""
    ev = _child_stops.get(result_id)
    if ev is None:
        return False
    ev.set()
    return True


def _compose_child_stop(result_id: str):
    """The child's stop predicate: its OWN per-child flag OR the coordinator's stop
    (D18, read from the run context). Either fires a clean child stop — so `Stop` on the
    coordinator also halts a running child instead of waiting it out."""
    flag = _child_stops.get(result_id)
    parent_stop = current_should_stop()

    def _stop() -> bool:
        if flag is not None and flag.is_set():
            return True
        return bool(parent_stop and parent_stop())
    return _stop


def run_prepared_events(plan: "SpawnPlan"):
    """Run a prepared child, YIELDING its events (E1) and recording its typed result.
    A generator: it yields the child's own `AgentEvent`s wrapped as `SubagentProgress`
    (the UI renders a live child), and via ``return`` hands back the prose render — the
    `spawn_subagent` tool_result the model reads. ALWAYS records a result — even on a
    raise — so every `SubagentStarted` gets a matching `SubagentFinished` (a running row
    never lingers). The child's stop predicate (its per-child flag ∨ the coordinator's)
    lets a user kill just this child (D2)."""
    last_check: CheckResult | None = None
    finished: Finished | None = None
    child = _run_child(
        plan.child_config, plan.child_messages,
        working_dir=str(plan.candidate_dir), run_key=plan.result_id,
        depth=plan.child_depth, should_stop=_compose_child_stop(plan.result_id),
    )
    try:
        # `yield from` streams the child's raw events (which we wrap) and captures the
        # generator's return value — (last_check, finished).
        while True:
            try:
                ev = next(child)
            except StopIteration as stop:
                last_check, finished = stop.value if stop.value is not None else (None, None)
                break
            yield SubagentProgress(plan.result_id, ev)
    except Exception as exc:  # noqa: BLE001 — a child failure is a tool result, never a parent crash
        # Record an error result too (D1), so the started child still finishes; the
        # tool_result the model reads stays the same explicit failure string.
        detail = f"{type(exc).__name__}: {exc}"
        _record_result(_error_result(plan, detail))
        return f"Error: subagent '{plan.subagent_type}' failed: {detail}"
    finally:
        # D2: drop the per-child stop flag now the child is done (registered in
        # prepare_spawn). Best-effort.
        _child_stops.pop(plan.result_id, None)
        # B1: reap the child's LSP file-workers now. Its scratch candidates are never
        # checked again (we collate via the filesystem into the parent's own canonical
        # file), so didClose every document under the child's scratch tree instead of
        # leaking one `lean --worker` per unique path. Best-effort.
        try:
            lsp_daemon.close_documents_under(str(plan.candidate_dir))
        except Exception:
            pass

    # `result_id` is the audit handle a promoted candidate links back to. Record the
    # typed result (with the child transcript) for the parent loop to drain into a
    # SubagentFinished event; return the prose render as the tool_result the model reads.
    result = _build_result(
        plan.result_id, plan.subagent_type, plan.child_depth,
        last_check, finished, plan.parent_wd,
    )
    _record_result(result)
    return result.render()


def run_prepared(plan: "SpawnPlan") -> str:
    """Non-streaming driver for direct/test callers (the registered `spawn_subagent`
    wrapper): exhaust `run_prepared_events`, discard the progress events, and return its
    render string. The coordinator loop instead `yield from`s the generator so the UI
    gets live progress; both share the one implementation (and its always-record and
    cleanup guarantees)."""
    gen = run_prepared_events(plan)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value
    finally:
        gen.close()


def run_children_concurrently(plans, *, max_children: int = DEFAULT_MAX_CONCURRENT_CHILDREN):
    """Run N prepared children CONCURRENTLY (E2), each on its own thread with an isolated
    COPY of the current context, merging all their live events up as they arrive.

    A generator: it yields every child's `SubagentProgress` (interleaved across children,
    in real arrival order) and, via ``return``, hands back ``{result_id: render_string}``
    — the coordinator maps each render back to its `spawn_subagent` tool call. The LLM
    call and `lean_check` are blocking I/O, so the GIL is released and the children truly
    overlap; a semaphore caps how many run at once (`max_children`) so a burst of spawns
    can't melt the box with parallel `lean --worker`s (the B1/B3 concern). Each child's
    context is a `contextvars.copy_context()`, so the per-run scopes (working_dir,
    should_stop, the result collector) are inherited but never trample each other, and a
    per-child exception is isolated into that child's render — a failing child never kills
    a sibling.

    Isolation-safety (distinct scratch dirs per child, per-uri LSP locking) is already
    paid for (items 21/27, B3); this only adds the execution overlap."""
    plans = list(plans)
    if not plans:
        return {}
    q: "_queue.Queue" = _queue.Queue()
    sem = threading.Semaphore(max(1, max_children))
    renders: dict[str, str] = {}
    threads = []

    def worker(plan, ctx):
        def run():
            render = None
            with sem:  # cap concurrent children; the rest queue here as threads
                try:
                    gen = run_prepared_events(plan)
                    try:
                        while True:
                            q.put(("event", next(gen)))
                    except StopIteration as stop:
                        render = stop.value
                except Exception as exc:  # noqa: BLE001 — isolate; never kill a sibling
                    render = (f"Error: subagent '{plan.subagent_type}' failed: "
                              f"{type(exc).__name__}: {exc}")
            q.put(("done", plan.result_id, render))
        # copy_context so this child inherits the coordinator's run context (working_dir,
        # should_stop, the shared result collector) yet gets its OWN mutable scope for the
        # child's run_events — concurrent children can't corrupt each other's ContextVars.
        ctx.run(run)

    for plan in plans:
        t = threading.Thread(target=worker, args=(plan, contextvars.copy_context()), daemon=True)
        t.start()
        threads.append(t)

    pending = len(plans)
    while pending > 0:
        kind, *rest = q.get()
        if kind == "event":
            yield rest[0]
        else:
            result_id, render = rest
            renders[result_id] = render
            pending -= 1
            # Report this child as FINISHED now, not when the batch is. The results
            # used to be drained by the coordinator only after `run_children_
            # concurrently` returned — i.e. after the SLOWEST sibling — so a child
            # that finished in 15s stayed 'running' for another 40s: its session view
            # showed a live "Checking with Lean…" spinner over a completed transcript,
            # and it counted as an active run the whole time. Measured on an 8-child
            # batch: every child retired within 30ms of the last one, ~51s after spawn.
            #
            # Draining here is safe because `drain_results` CLEARS what it returns, so
            # the coordinator's post-batch drain simply finds nothing left. It may also
            # pick up a sibling that finished microseconds earlier — reporting that one
            # slightly early is still true, and still in completion order.
            for result in drain_results():
                yield result.to_event()
    for t in threads:
        t.join()
    return renders


@tool(name="spawn_subagent", description=_SPAWN_SCHEMA["description"],
      input_schema=_SPAWN_SCHEMA["input_schema"], opt_in=True,
      schema_factory=build_spawn_schema)
def spawn_subagent(args: dict) -> str:
    """Thin wrapper preserving the registered-tool contract for direct/test callers.
    The coordinator loop (agent.py) instead calls `prepare_spawn` then streams via
    `run_prepared_events`, so it can emit `SubagentStarted` + live progress (D1/E1)."""
    plan = prepare_spawn(args)
    if isinstance(plan, str):
        return plan
    return run_prepared(plan)
