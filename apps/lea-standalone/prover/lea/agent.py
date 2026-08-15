"""Lea agent — the core loop. Model calls tools until done.

`run_events()` is the generator core: it yields a typed event stream and never
prints. `run()` is a backward-compatible wrapper that drains those events through
the default stdout renderer and returns the final text (and optional transcript),
so existing callers (CLI, eval) keep working unchanged.
"""

import contextvars
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import LeaConfig
from . import condenser
from . import diagnostics
from .runctx import run_context
from .prompt import compose_role_prompt, domain_cascade_hint, load_system_prompt
from .providers import stream, TextDelta, ToolCall, Done, _ToolMeta, Usage
from . import safeverify
from . import subagents  # subagent-result collector (item 22); also registers spawn_subagent via tools
from . import tools as _tools  # noqa: F401 — importing registers the built-in tools
from .tools import (
    _lean_check_has_error, _lean_check_has_sorry, _first_error_line, _tool_result_ok,
    _run_relative_path,
)
from .registry import build_toolset, import_tool_modules, pop_scope, push_scope
from .events import (
    TurnStarted,
    AssistantTextDelta,
    ToolCalled,
    ToolResulted,
    ToolApprovalRequested,
    FileChanged,
    CheckResult,
    Diagnostic,
    UsageUpdated,
    Compacted,
    SubagentStarted,
    Finished,
)

# E3: read-only tools with no side effects and no ordering constraints — safe to run
# CONCURRENTLY when a turn issues several at once (e.g. F1's multi-modal search fires
# Loogle + semantic + grep together). Anything that writes, checks, or shells out
# (write_file/edit_file/lean_check/bash) stays serial and in order — a write→check pair
# must never be reordered. `spawn_subagent` has its own concurrency path (E2).
_PARALLEL_SAFE_TOOLS = frozenset({"read_file", "search_mathlib"})


_NARRATE_TOOL_STEPS_INSTRUCTION = """

When you are about to call one or more tools, first write a concise progress
summary for the user. Keep it to one or two sentences, use Markdown when helpful,
and include mathematical notation in normal LaTeX delimiters when useful. Explain
what you are trying next and why, then call the tool. Do not narrate after every
minor token or repeat boilerplate; summarize the meaningful proof step.
"""


_FORCED_TOOL_NARRATION_INSTRUCTION = """

You are Lea explaining the next proof action to the user. The main model turn
selected a tool call without first writing user-facing narration. Write the
missing narration now.

Rules:
- Write one concise paragraph unless the mathematical plan genuinely benefits
  from a short numbered list.
- Explain the mathematical or Lean proof move being attempted and why it is the
  next useful step.
- Use Markdown and ordinary LaTeX delimiters when helpful.
- Do not mention JSON, API internals, or hidden/private reasoning.
- Do not call tools. Return only the narration text.
"""


_INTENT_CLASSIFIER_PROMPT = """\
You route the user's latest message in an interactive Lean 4 session that already \
contains prior work (often a completed proof).

Reply with exactly one word:
- FORMALIZE — the user is giving you a NEW mathematical statement to formalize and \
prove, is otherwise asking you to prove/formalize something, OR is approving/confirming a \
formalization you just proposed (e.g. "go ahead", "yes, prove it", "sounds good", "that's \
right") so you should now carry out the proof.
- ASSISTANT — the user is asking a question, requesting an explanation, asking you to \
look up a lemma in Mathlib, clarifying a tactic, or otherwise continuing the \
conversation about existing work.

Output only the single word FORMALIZE or ASSISTANT, with no other text."""


_RESULT_CLASSIFIER_PROMPT = """\
You classify the mathematical outcome of a verified Lean artifact for Lea.

The Lean artifact has already passed the final checker. Your job is only to decide
what it verified relative to the user's original request.

Reply with exactly one word:
- PROVED — the artifact proves the user's stated theorem/lemma/conjecture as written.
- DISPROVED — the artifact proves a negation, contradiction, or counterexample showing
  the user's stated theorem/lemma/conjecture is false.
- NEEDS_REVIEW — the relationship is ambiguous, the artifact proves a related but
  different statement, or you are not certain.

Be conservative. If the user asked for a counterexample and the artifact verifies
one, reply DISPROVED. If the user asked for a proof but the artifact verifies the
negation or a counterexample, reply DISPROVED. If unsure, reply NEEDS_REVIEW.

Output only PROVED, DISPROVED, or NEEDS_REVIEW."""


def _text_only_history(messages: list, limit: int = 8) -> list[dict]:
    """Flatten messages to a cheap text-only {role, content} list for classification.

    Drops tool calls/results and assistant tool-call parts so the classifier sees a
    clean conversation; keeps only the trailing `limit` text turns.
    """
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            text = ""
        text = text.strip()
        if not text:
            continue
        if role == "assistant":
            # Assistant turns must use the parts format: _to_openai_messages treats a
            # bare string as a user-only shape and iterates assistant content as a list.
            out.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        else:
            out.append({"role": "user", "content": text})
    return out[-limit:]


_MAX_TURNS_SUMMARY_INSTRUCTION = (
    "You have reached your turn budget without finishing. You have NO tools now — do not "
    "attempt any more actions. Write a concise final report for whoever delegated this to "
    "you:\n"
    "  - what you found or accomplished, concretely: lemma names with signatures, file "
    "paths, the closest working proof state, or the exact error still blocking progress;\n"
    "  - the single most promising next step.\n"
    "This report is your entire deliverable — the turns you spent are only worth something "
    "if you hand back what you learned. Make it useful evidence, not an apology."
)


def _summarize_on_max_turns(model: str, system: str, messages: list, config: LeaConfig
                            ) -> tuple[str, Usage, float]:
    """One final, tool-less turn when the budget is exhausted (mirrors opencode's
    summarize-on-cap): ask the model to hand back its findings + best next step, so a
    capped run returns useful evidence instead of a discarded "max turns" error — which
    matters most for a delegated sub-agent, whose summary IS its deliverable to the
    coordinator.

    Built on the same clean, tool-stripped history the classifiers use (provider-safe:
    no dangling tool_use, proper alternation), with a generous window so the whole run is
    in view. Returns (summary_text, usage, cost)."""
    history = _text_only_history(messages, limit=60)
    # Deliver the instruction as the final user turn — merged into a trailing user message
    # rather than appended after it, so we never emit two consecutive user turns (which
    # some providers reject).
    if history and history[-1].get("role") == "user":
        history[-1] = {"role": "user",
                       "content": f"{history[-1]['content']}\n\n{_MAX_TURNS_SUMMARY_INSTRUCTION}"}
    else:
        history.append({"role": "user", "content": _MAX_TURNS_SUMMARY_INSTRUCTION})
    text = ""
    usage = Usage()
    cost = 0.0
    for event in stream(model, system, history, [], config.model_kwargs, streaming=config.stream):
        if isinstance(event, TextDelta):
            text += event.text
        elif isinstance(event, Done):
            usage.input_tokens += event.usage.input_tokens
            usage.output_tokens += event.usage.output_tokens
            cost += event.cost
    return text.strip(), usage, cost


def _classify_intent(model: str, messages: list, config: LeaConfig) -> tuple[str, Usage, float]:
    """Ask the model whether the latest turn is FORMALIZE or ASSISTANT.

    Returns (decision, usage, cost). Defaults to FORMALIZE on any ambiguity so a
    genuine theorem to prove is never silently dropped into chat mode.
    """
    history = _text_only_history(messages)
    text = ""
    usage = Usage()
    cost = 0.0
    for event in stream(
        model,
        _INTENT_CLASSIFIER_PROMPT,
        history,
        [],
        config.model_kwargs,
        streaming=config.stream,
    ):
        if isinstance(event, TextDelta):
            text += event.text
        elif isinstance(event, Done):
            usage.input_tokens += event.usage.input_tokens
            usage.output_tokens += event.usage.output_tokens
            cost += event.cost
    decision = "ASSISTANT" if "ASSISTANT" in text.strip().upper() else "FORMALIZE"
    return decision, usage, cost


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif part.get("type") == "tool_result":
                continue
        return " ".join(p for p in parts if p)
    return ""


def _latest_user_request(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        text = _content_text(msg.get("content")).strip()
        if text:
            return text
    return ""


def _theorem_signature(path: str | None) -> str:
    if not path:
        return ""
    try:
        return safeverify.theorem_signature(Path(path).read_text()) or ""
    except Exception:
        return ""


def _parse_result_kind(text: str) -> str:
    value = (text or "").strip().upper()
    if "DISPROVED" in value:
        return "disproved"
    if "PROVED" in value:
        return "proved"
    if "NEEDS_REVIEW" in value or "NEEDS REVIEW" in value:
        return "needs_review"
    return "needs_review"


def _classify_final_result(
    *,
    model: str,
    messages: list,
    final_text: str,
    theorem_signature: str,
    config: LeaConfig,
) -> tuple[str, str | None, Usage, float]:
    """Classify a checked artifact relative to the user's request.

    This is intentionally conservative: parse failures, provider errors, or any
    non-conforming answer become needs_review, not proof success.
    """
    request = _latest_user_request(messages)
    classifier_messages = [{
        "role": "user",
        "content": (
            "User request:\n"
            f"{request or '(unavailable)'}\n\n"
            "Verified Lean theorem signature:\n"
            f"{theorem_signature or '(unavailable)'}\n\n"
            "Final assistant message:\n"
            f"{final_text or '(unavailable)'}"
        ),
    }]
    text = ""
    usage = Usage()
    cost = 0.0
    try:
        for event in stream(
            model,
            _RESULT_CLASSIFIER_PROMPT,
            classifier_messages,
            [],
            config.model_kwargs,
            streaming=config.stream,
        ):
            if isinstance(event, TextDelta):
                text += event.text
            elif isinstance(event, Done):
                usage.input_tokens += event.usage.input_tokens
                usage.output_tokens += event.usage.output_tokens
                cost += event.cost
    except Exception as exc:
        return "needs_review", f"Result classification failed: {type(exc).__name__}: {exc}", usage, cost
    kind = _parse_result_kind(text)
    detail = text.strip() or None
    return kind, detail, usage, cost


def _domain_cascade_for_check(args: dict, working_dir: str | None, surfaced: set[str]) -> str | None:
    """The domain-scoped tactic hint (item 26) for the file a `lean_check` targeted, or None.

    Reads the checked file's text — resolving a relative path against this activation's
    `working_dir` first (two concurrent runs don't share a per-run cwd), then as given — and
    asks `domain_cascade_hint` for the fragments whose domain is present and not yet surfaced.
    Best-effort: any read failure yields None (a missing hint never breaks a check)."""
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return None
    p = Path(path).expanduser()
    candidates = [p] if p.is_absolute() else (
        [Path(working_dir).expanduser() / p, p] if working_dir else [p]
    )
    for cand in candidates:
        try:
            text = cand.read_text()
        except OSError:
            continue
        return domain_cascade_hint(text, surfaced)
    return None


def _meaning_events(tool_name: str, args: dict, result: str) -> list:
    """Map one finished tool call to the meaning-level events the adapter acts on
    (D17). Reuses the same classification as ProofVerificationState.note_tool_result
    so the event stream and the agent's proof-state can never disagree.

    Scope (A2): FileChanged (on .lean writes) + CheckResult (on lean_check).
    VerifyResult arrives with the verify capability (A6); Error with the bridge.
    """
    raw = args.get("path")
    if not isinstance(raw, str) or not raw:
        return []
    # Report the path the tool ACTUALLY used, not the string the model typed. These
    # events cross a boundary: the adapter reads the file back from `FileChanged`, and
    # a subagent's candidate is collated from `CheckResult.path` — both of which run
    # elsewhere, where a bare "candidate.lean" resolves against the process cwd (the
    # adapter's own directory) and finds nothing.
    #
    # That is exactly how a compiling sub-agent proof was silently discarded: the child
    # wrote and checked `candidate.lean` correctly inside its scratch dir, but the event
    # carried the unresolved name, `_relativize` could not place it under the parent's
    # working dir, and the adapter looked for it in the session root. No candidate, no
    # code step, no error — just a child that appeared to have produced nothing.
    path = str(_run_relative_path(raw))
    if tool_name in {"write_file", "edit_file"}:
        if path.endswith(".lean") and _tool_result_ok(result):
            return [FileChanged(path)]
        return []
    if tool_name == "lean_check":
        err = _lean_check_has_error(result)
        return [CheckResult(path, "error" if err else "ok",
                            _first_error_line(result) if err else None)]
    return []


class ProofVerificationState:
    """Track whether the latest Lean proof file has passed a fresh check."""

    def __init__(self):
        self.latest_proof_path: str | None = None
        self.unchecked_write = False
        self.latest_check_path: str | None = None
        self.latest_check_output: str | None = None
        self.latest_check_passed: bool | None = None

    def note_tool_result(self, tool_name: str, args: dict, result: str) -> None:
        raw = args.get("path")
        if not isinstance(raw, str) or not raw:
            return
        # Resolved, for the same reason `_meaning_events` resolves: `latest_proof_path`
        # is later fed back to `lean_check` by the final gate and to
        # `_theorem_signature`, and the `path == self.latest_proof_path` comparison
        # below silently fails when the model writes `candidate.lean` and then checks
        # `./candidate.lean` — two spellings of one file, so a checked proof reads as
        # an unchecked write.
        path = str(_run_relative_path(raw))
        if tool_name in {"write_file", "edit_file"}:
            if _tool_result_ok(result) and path.endswith(".lean"):
                self.latest_proof_path = path
                self.unchecked_write = True
            return
        if tool_name == "lean_check":
            # A proof is only verified if it compiles with no errors AND no `sorry`
            # — `sorry` is a warning, so checking errors alone would accept a
            # skeleton as a finished proof (a false "Proved").
            passed = not _lean_check_has_error(result) and not _lean_check_has_sorry(result)
            self.latest_check_path = path
            self.latest_check_output = result
            self.latest_check_passed = passed
            if path == self.latest_proof_path:
                self.unchecked_write = False

    def needs_final_check(self) -> bool:
        if not self.latest_proof_path:
            return False
        return (
            self.unchecked_write
            or self.latest_check_path != self.latest_proof_path
        )

    def latest_proof_verified(self) -> bool:
        return (
            bool(self.latest_proof_path)
            and not self.unchecked_write
            and self.latest_check_path == self.latest_proof_path
            and self.latest_check_passed is True
        )


_FINAL_GATE_FAILURE_MESSAGE = (
    "Error: final verification gate failed. Lea claimed the proof was complete, "
    "but the latest proof file did not pass lean_check."
)

_NO_PROOF_ARTIFACT_MESSAGE = (
    "Error: no proof artifact was produced. You must create a complete .lean file "
    "with the write_file tool, then run lean_check on that file. Do not finish with "
    "only prose or a Markdown code block."
)


def _tool_call_for_prompt(name: str, args: dict) -> dict:
    """Compact a tool call enough to show a narration-only model pass."""
    compact: dict = {}
    for key, value in args.items():
        if isinstance(value, str):
            if key == "content" and len(value) > 1600:
                compact[key] = value[:1600] + "\n... [truncated]"
            elif len(value) > 800:
                compact[key] = value[:800] + "... [truncated]"
            else:
                compact[key] = value
        else:
            compact[key] = value
    return {"name": name, "args": compact}


def _forced_tool_narration(
    *,
    model: str,
    system: str,
    messages: list,
    tool_name: str,
    tool_args: dict,
    config: LeaConfig,
):
    """Ask the model for narration when a tool-only turn would otherwise be silent."""
    narration_messages = messages + [{
        "role": "user",
        "content": (
            "Write the user-facing narration that should appear immediately "
            "before this Lea tool call:\n"
            f"{json.dumps(_tool_call_for_prompt(tool_name, tool_args), ensure_ascii=False, indent=2)}"
        ),
    }]
    text = ""
    usage = Usage()
    cost = 0.0
    try:
        for event in stream(
            model,
            system + _FORCED_TOOL_NARRATION_INSTRUCTION,
            narration_messages,
            [],
            config.model_kwargs,
            streaming=config.stream,
        ):
            if isinstance(event, TextDelta):
                text += event.text
                yield event
            elif isinstance(event, Done):
                usage.input_tokens += event.usage.input_tokens
                usage.output_tokens += event.usage.output_tokens
                cost += event.cost
    except Exception:
        fallback = _fallback_tool_narration(tool_name, tool_args)
        text += fallback
        yield TextDelta(fallback)
    return text.strip(), usage, cost


def _fallback_tool_narration(tool_name: str, args: dict) -> str:
    path = args.get("path")
    if tool_name == "write_file":
        if isinstance(path, str) and path:
            return f"I will write the next Lean proof attempt in `{path}` and then check whether it compiles."
        return "I will write the next Lean proof attempt and then check whether it compiles."
    if tool_name == "edit_file":
        if isinstance(path, str) and path:
            return f"I will revise `{path}` to address the previous Lean feedback, then re-run the checker."
        return "I will revise the Lean proof to address the previous checker feedback, then re-run it."
    if tool_name == "lean_check":
        if isinstance(path, str) and path:
            return f"I will run Lean on `{path}` to verify the current proof and inspect any errors."
        return "I will run Lean to verify the current proof and inspect any errors."
    if tool_name == "search_mathlib":
        query = args.get("query")
        if isinstance(query, str) and query:
            return f"I will search Mathlib for lemmas related to `{query}` so the next proof step can use existing results."
        return "I will search Mathlib for a relevant lemma before continuing the proof."
    if tool_name == "suggest_imports":
        if isinstance(path, str) and path:
            return f"I will analyze `{path}` and replace broad or redundant imports with targeted modules."
        return "I will analyze the proof's imports and identify a targeted replacement block."
    return f"I will use `{tool_name}` for the next proof step and then use its result to continue."


def run_events(
    config: LeaConfig,
    messages: list,
    *,
    namespace: str | None = None,
    session_id: str | None = None,
    working_dir: str | None = None,
    should_stop=None,
    gate=None,
    depth: int = 0,
):
    """Core loop as a generator: yields typed events, never prints.

    **Stateless (D16):** one activation = one pure call. The caller passes the full
    conversation `messages` (the transcript it owns in the DB), and the prover holds
    nothing between activations — no disk persistence, no resume-from-disk. The
    final messages ride back out in the `Finished` event's transcript.

    Yields per turn: TurnStarted, AssistantTextDelta*, ToolCalled*, UsageUpdated,
    ToolResulted* (+ meaning-level FileChanged/CheckResult), and finally Finished.

    **Per-tool gate (D19):** `gate` is an optional callable `(tool_name, args) -> bool`
    supplied by the adapter; True means "this tool needs human approval." Before
    running such a tool the loop yields a two-way `ToolApprovalRequested`; the caller
    answers with `gen.send('allow' | 'always_session' | 'deny')`. The gated set + the
    session allowlist are the adapter's policy (it owns `always_session`); run_events
    only asks and honors the answer. `gate=None` → no gating.

    **Interrupt (D18):** `should_stop` is an optional `() -> bool` the caller flips to
    request a stop. It's checked at each turn boundary (one turn = one UI step), so the
    stop is *cooperative and clean* — the current step's write is already committed and
    the canvas is accurate — not a hard kill. On stop the loop yields a terminal
    `Finished("interrupted", ...)` carrying the transcript so far (so a follow-up still
    has a coherent base). An in-flight turn is not interrupted mid-flight; the stop
    lands at the next turn boundary.

    Owns MCP lifecycle: starts configured servers (which register their tools)
    before the inner loop resolves the toolset, and stops them when the event
    stream ends or is closed.
    """
    # Open this activation's tool-registry overlay (item 27) BEFORE starting MCP, so the
    # MCP tools register into *this run's* layer (on the caller thread) rather than the
    # process-global registry — two concurrent MCP-enabled runs then can't corrupt each
    # other's toolsets. Popped in the finally, dropping the run's dynamic tools with it.
    registry_scope = push_scope()
    # F2: declared HTTP tools register into the same per-activation overlay MCP uses, so
    # they are ordinary tools to the loop and vanish when the run ends.
    if config.http_tools:
        from .http_tools import register_http_tools

        register_http_tools(config.http_tools)
    mcp_manager = None
    if config.mcp_servers:
        # A8: acquire a POOLED connection rather than spawning a server per run. The
        # tools still register into THIS activation's overlay (dropped at `pop_scope`
        # below) — only the connection is shared, so a warm Lean server survives the
        # run boundary instead of costing ~35s again on the next message.
        from .mcp import acquire
        mcp_manager = acquire(config.mcp_servers)
        if mcp_manager is not None:
            mcp_manager.register_discovered()
        # v2.5 A3 — a server that failed to start used to be a stderr warning only, so the
        # run continued with zero MCP tools and the user saw a feature that silently did
        # nothing. Yield the failures DIRECTLY rather than via `diagnostics.report`: the
        # diagnostics scope opens later (below), and the loop's own drain runs in Phase 4
        # after tool calls — so a reported startup error would surface late, or never on a
        # run that makes no tool calls. `severity='degraded'` because the capability is
        # reduced *and stays* reduced for the whole run.
        from .mcp import summarize_stderr
        # G3 — the positive assertion. A server that connects and lists NOTHING raises no
        # exception, so without asking "did each configured server actually contribute?"
        # the run proceeds with a capability the user believes is on and isn't. This is
        # the absence-failure shape: nothing to catch, so something must check.
        if mcp_manager.is_alive():
            failed = {e["server"] for e in mcp_manager.startup_errors}
            contributing = {d["server"] for d in mcp_manager._discovered}
            for name in config.mcp_servers:
                if name in failed or name in contributing:
                    continue
                yield Diagnostic(
                    severity="degraded",
                    code="mcp.no_tools",
                    message=(
                        f"MCP server {name!r} started but offered no tools, so it adds "
                        f"nothing to this run."
                    ),
                    source="mcp",
                    context={"server": name},
                )
        for err in mcp_manager.startup_errors:
            # The headline is the ONE line that says what to fix; the full tail rides
            # along in `context` for anyone who wants it. Dumping a raw traceback at
            # the user is what this whole slice exists to stop.
            reason = summarize_stderr(err["stderr_tail"]) or err["message"]
            yield Diagnostic(
                severity="degraded",
                code="mcp.server_failed",
                message=(
                    f"MCP server {err['server']!r} did not start, so its tools are "
                    f"unavailable for this run. {reason}"
                ),
                source="mcp",
                context={"server": err["server"], "transport": err["transport"],
                         "reason": reason, "detail": err["stderr_tail"] or err["message"]},
            )
    # Establish the per-activation run context (item 8) for the whole event
    # stream: `working_dir` so filesystem tools (bash) act in this run's tree
    # instead of the process-global cwd, and `run_key` (session id) for the
    # lock/scratch keys later items build on. It wraps the `yield from`, so the
    # ContextVars stay set through every tool call delegated to the inner loop.
    run_key = session_id or uuid.uuid4().hex[:12]
    try:
        # `depth` (item 18) records this activation's nesting; `config` is stashed so
        # spawn_subagent can derive a child config. Both ride the ContextVars through
        # every tool call the inner loop delegates.
        with run_context(working_dir=working_dir, run_key=run_key, depth=depth, config=config,
                         should_stop=should_stop):
            # A fresh subagent-result collector per activation (item 22): spawn_subagent
            # records here, the inner loop drains into SubagentFinished events. Scoped so
            # results can't leak across runs; a child opens its own empty scope.
            results_token = subagents.begin_results_scope()
            # v2.4: the same scoping for human-facing diagnostics — any tool can
            # `diagnostics.report(...)` and the loop drains it into Diagnostic events.
            # Per-activation so a child's degraded-capability notices are its own.
            diag_token = diagnostics.begin_scope()
            try:
                yield from _run_events_inner(
                    config, messages, namespace=namespace, session_id=session_id,
                    working_dir=working_dir, should_stop=should_stop, gate=gate,
                )
            finally:
                diagnostics.end_scope(diag_token)
                subagents.end_results_scope(results_token)
    finally:
        # A8: do NOT stop the manager — the pool owns its lifetime, and stopping it here
        # is exactly what made every run pay the cold-start again. `pop_scope` drops this
        # activation's tool registrations, which is all this run owned.
        pop_scope(registry_scope)


def _run_events_inner(
    config: LeaConfig,
    messages: list,
    *,
    namespace: str | None = None,
    session_id: str | None = None,
    working_dir: str | None = None,
    should_stop=None,
    gate=None,
):
    # `namespace` (e.g. "Lea.Foo") lets the adapter state the active write
    # namespace for a project run (D32); None keeps the default Lea.Misc block.
    system = load_system_prompt(
        config.prompt_variant, config.skills, workspace=working_dir, namespace=namespace,
    )
    if config.narrate_tool_steps:
        system += _NARRATE_TOOL_STEPS_INSTRUCTION
    # A subagent role head (item 19) is composed onto the shared Lean core and
    # BRACKETED by a non-negotiable reassertion of the hard rules (item 20), so a role
    # can specialize but can never override "never modify the statement" / no
    # sorry/axiom — even as the last instruction. No head → core unchanged.
    system = compose_role_prompt(system, config.system_prompt_head)
    model = config.model

    # Resolve the active toolset once: import any user tool modules so their
    # tools register, then select per config (None → all registered tools).
    import_tool_modules(config.tool_modules)
    tools_schema, tool_handlers = build_toolset(config.tools, config.extra_tools)

    # Stateless (D16): the caller owns the transcript. Work on a private copy so we
    # never mutate the caller's list in place; the final state rides out via the
    # Finished transcript. Per-activation usage starts at zero — the adapter
    # accumulates across activations. The adapter owns session identity; we only
    # generate a label if it didn't pass one (e.g. a standalone/test call).
    messages = list(messages)
    if session_id is None:
        session_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    total_usage = Usage()

    total_cost = 0.0
    proof_state = ProofVerificationState()

    def transcript(turns: int) -> dict:
        clean = []
        for msg in messages:
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                clean.append({"role": "assistant", "content": [
                    {k: v for k, v in item.items() if k != "raw_part"}
                    for item in msg["content"]
                ]})
            else:
                clean.append(msg)
        return {
            "session_id": session_id,
            "model": model,
            "turns": turns,
            "usage": {"input_tokens": total_usage.input_tokens, "output_tokens": total_usage.output_tokens},
            "messages": clean,
        }

    # Interactive chat: classify whether this turn is a new formalization or an
    # assistant/QA request (explain, look up a lemma, etc.), and route. Only fires
    # when the conversation already has a prior assistant turn — a first turn is
    # always a formalization, so the cold-start path is untouched. (Keying off a
    # prior assistant turn, not message count, stays correct even when the caller
    # prepends project context to a fresh run.)
    assistant_mode = False
    if config.prompt_variant == "interactive" and any(m.get("role") == "assistant" for m in messages):
        decision, intent_usage, intent_cost = _classify_intent(model, messages, config)
        total_usage.input_tokens += intent_usage.input_tokens
        total_usage.output_tokens += intent_usage.output_tokens
        total_cost += intent_cost
        if intent_usage.input_tokens or intent_usage.output_tokens or intent_cost:
            yield UsageUpdated(intent_usage.input_tokens, intent_usage.output_tokens, intent_cost)
        assistant_mode = decision == "ASSISTANT"

    # Domains whose tactic cascade has already been surfaced this activation (item 26),
    # so a domain hint rides a lean_check result once, not on every check.
    surfaced_domains: set[str] = set()

    turn = 0
    # The real input-token count from the previous turn's provider `Done` — the compaction
    # trigger signal (G1). 0 on the first turn (nothing sent yet → nothing to compact).
    last_input_tokens = 0
    while True:
        # Cooperative interrupt (D18): the human asked to stop. `turn` is the count
        # of completed turns, so the last step's write is already committed and the
        # canvas is accurate — a clean stop, not a hard kill. The transcript so far
        # rides out so a follow-up still has a coherent base.
        if should_stop is not None and should_stop():
            yield Finished("interrupted", "Run interrupted by the user.",
                           turn, session_id, model, total_usage, total_cost, transcript(turn))
            return

        # Context compaction (G1): before spending another turn, if the last turn's real
        # input size crossed the trigger, condense the model-facing history — prune
        # superseded tool outputs, then summarize the older middle only if that's not
        # enough. Keeps the coordinator's context bounded on a long run without losing the
        # goal or the recent state. The condenser is copy-on-write; `messages` is rebound to
        # the condensed history the next `stream` call sends. Only the model context shrinks —
        # the durable record the adapter keeps is built from the event stream, not from here.
        if condenser.should_compact(last_input_tokens, config):
            result = condenser.condense(messages, config, model=model,
                                        last_input_tokens=last_input_tokens)
            if result.changed:
                messages = result.messages
                total_usage.input_tokens += result.usage.input_tokens
                total_usage.output_tokens += result.usage.output_tokens
                total_cost += result.cost
                if result.usage.input_tokens or result.usage.output_tokens or result.cost:
                    yield UsageUpdated(result.usage.input_tokens, result.usage.output_tokens,
                                       result.cost)
                yield Compacted(result.before_tokens, result.after_tokens,
                                result.pruned, result.summarized)
                # Reflect the condensed size so we don't immediately re-trigger next turn on
                # the stale pre-compaction count; the next real `Done` refreshes it anyway.
                last_input_tokens = result.after_tokens

        turn += 1
        # D6: a per-run cost cap ends the run the same clean way the turn cap does. Checked
        # at the turn boundary against the spend so far (a sub-agent's `max_cost` override
        # flows here via `max_cost_usd`); None → uncapped, so top-level runs are unaffected.
        cost_capped = config.max_cost_usd is not None and total_cost >= config.max_cost_usd
        if (config.max_turns and turn > config.max_turns) or cost_capped:
            # Budget exhausted. Don't discard the work with a canned error — spend one
            # final tool-less turn asking the model to summarize its findings + best next
            # step, and return THAT as the result. For a delegated sub-agent this is the
            # difference between handing the coordinator useful evidence and handing it
            # nothing. Defensive: a failure in the summary call degrades to a plain notice
            # rather than turning a completed run into an error.
            try:
                summary, s_usage, s_cost = _summarize_on_max_turns(model, system, messages, config)
            except Exception:  # noqa: BLE001 — a terminal-path best effort, never fatal
                summary, s_usage, s_cost = "", Usage(), 0.0
            total_usage.input_tokens += s_usage.input_tokens
            total_usage.output_tokens += s_usage.output_tokens
            total_cost += s_cost
            if s_usage.input_tokens or s_usage.output_tokens or s_cost:
                yield UsageUpdated(s_usage.input_tokens, s_usage.output_tokens, s_cost)
            if summary:
                # Surface it live + fold it into the transcript, so both the UI and the
                # materialized sub-agent view carry the findings as the final message.
                yield AssistantTextDelta(summary)
                # Parts shape, like every other assistant turn: the transcript is
                # persisted and replayed on the next activation, where
                # _to_openai_messages iterates assistant content as a list of blocks.
                messages.append({"role": "assistant", "content": [{"type": "text", "text": summary}]})
            final_text = summary or (
                "Reached the cost budget without completing the task." if cost_capped
                else "Reached the turn budget without completing the task."
            )
            yield Finished("max_turns", final_text,
                           turn - 1, session_id, model, total_usage, total_cost, transcript(turn - 1))
            return

        yield TurnStarted(turn)

        assistant_parts = []
        current_text = ""
        tool_calls = []
        reasoning_items = []
        forced_narration_emitted = False

        for event in stream(model, system, messages, tools_schema, config.model_kwargs, streaming=config.stream):
            if isinstance(event, TextDelta):
                current_text += event.text
                yield AssistantTextDelta(event.text)
            elif isinstance(event, ToolCall):
                if config.narrate_tool_steps and not forced_narration_emitted and not current_text and not any(
                    part.get("type") == "text" and part.get("text") for part in assistant_parts
                ):
                    narration = _forced_tool_narration(
                        model=model,
                        system=system,
                        messages=messages,
                        tool_name=event.name,
                        tool_args=event.args,
                        config=config,
                    )
                    try:
                        while True:
                            narration_event = next(narration)
                            current_text += narration_event.text
                            yield AssistantTextDelta(narration_event.text)
                    except StopIteration as result:
                        _, narration_usage, narration_cost = result.value
                        total_usage.input_tokens += narration_usage.input_tokens
                        total_usage.output_tokens += narration_usage.output_tokens
                        total_cost += narration_cost
                        if narration_usage.input_tokens or narration_usage.output_tokens or narration_cost:
                            yield UsageUpdated(
                                narration_usage.input_tokens,
                                narration_usage.output_tokens,
                                narration_cost,
                            )
                    forced_narration_emitted = True
                if current_text:
                    assistant_parts.append({"type": "text", "text": current_text})
                    current_text = ""
                yield ToolCalled(event.name, event.args)
                tool_calls.append({"name": event.name, "args": event.args, "id": None, "raw_part": event.raw_part})
            elif isinstance(event, _ToolMeta):
                if tool_calls:
                    tool_calls[-1]["id"] = event.tool_use_id
            elif isinstance(event, Done):
                reasoning_items = event.reasoning_items
                total_usage.input_tokens += event.usage.input_tokens
                total_usage.output_tokens += event.usage.output_tokens
                total_cost += event.cost
                # The real context size of this turn — the G1 compaction trigger for next turn.
                last_input_tokens = event.usage.input_tokens
                yield UsageUpdated(event.usage.input_tokens, event.usage.output_tokens, event.cost)

        if current_text:
            assistant_parts.append({"type": "text", "text": current_text})
        if reasoning_items:
            # Internal continuation state for the Responses API.  It is persisted
            # with this exact assistant turn but never rendered as user-facing text.
            assistant_parts.append({"type": "reasoning", "items": reasoning_items})
        for tc in tool_calls:
            assistant_parts.append({
                "type": "tool_call",
                "name": tc["name"],
                "args": tc["args"],
                "id": tc["id"],
                "raw_part": tc.get("raw_part"),
            })
        messages.append({"role": "assistant", "content": assistant_parts})

        if not tool_calls:
            text = "".join(p["text"] for p in assistant_parts if p["type"] == "text")
            if assistant_mode:
                # Chat/assistant turn — not a formalization run, so skip the
                # final proof gate entirely.
                yield Finished("assistant", text or "(no response)", turn, session_id, model,
                               total_usage, total_cost, transcript(turn))
                return
            if not proof_state.latest_proof_path:
                # Interactive collaborator: a text-only turn before any proof file
                # exists means the model is presenting its plan / natural-language
                # sketch and waiting for the user — the prompt tells it to lead with
                # the math and not touch files until the user confirms. Let the run
                # pause here instead of forcing a proof artifact. Non-interactive
                # variants (eval/default) keep the nudge so they never stop short.
                if config.prompt_variant == "interactive":
                    yield Finished("assistant", text or "(no response)", turn, session_id,
                                   model, total_usage, total_cost, transcript(turn))
                    return
                messages.append({"role": "user", "content": _NO_PROOF_ARTIFACT_MESSAGE})
                continue
            if proof_state.needs_final_check():
                check_path = proof_state.latest_proof_path
                assert check_path is not None
                check_args = {"path": check_path}
                yield ToolCalled("lean_check", check_args)
                handler = tool_handlers.get("lean_check")
                if handler:
                    try:
                        result = handler(check_args)
                    except Exception as e:
                        result = f"Error: tool 'lean_check' raised {type(e).__name__}: {e}"
                        diagnostics.report(
                            "step_error", "tool.raised",
                            f"The final verification check failed to run: {type(e).__name__}: {e}",
                            tool="lean_check", turn=turn, path=check_path,
                        )
                else:
                    result = "Error: unknown tool 'lean_check'"
                    diagnostics.report(
                        "step_error", "tool.unknown",
                        "The final verification check could not run: lean_check is not "
                        "in this run's toolset.",
                        tool="lean_check", turn=turn, path=check_path,
                    )
                preview = result[:200] + "..." if len(result) > 200 else result
                yield ToolResulted("lean_check", result, preview)
                for ev in _meaning_events("lean_check", check_args, result):
                    yield ev
                # The final gate runs AFTER Phase 4's drain, so anything it reported
                # would otherwise sit in the collector until the next turn — and on the
                # last turn, be dropped entirely.
                for diag in diagnostics.drain():
                    yield diag
                proof_state.note_tool_result("lean_check", check_args, result)

                tool_result = {"type": "tool_result", "tool_name": "lean_check", "content": result}
                gate_call_id = f"final_gate_lean_check_{turn}"
                messages.append({"role": "assistant", "content": [{
                    "type": "tool_call",
                    "name": "lean_check",
                    "args": check_args,
                    "id": gate_call_id,
                }]})
                tool_result["tool_use_id"] = gate_call_id
                tool_result["tool_call_id"] = gate_call_id
                messages.append({"role": "user", "content": [tool_result]})
            if proof_state.latest_proof_path and not proof_state.latest_proof_verified():
                if config.prompt_variant == "interactive":
                    # Collaborator: the proof isn't actually done (errors, or still
                    # has a `sorry` — e.g. a skeleton the agent is asking you about).
                    # End cleanly as a chat turn — never a false "Proved" — and let
                    # the user steer the next step instead of barreling ahead.
                    yield Finished("assistant", text or "(no response)", turn, session_id,
                                   model, total_usage, total_cost, transcript(turn))
                    return
                diagnostic = proof_state.latest_check_output or "No successful lean_check was observed."
                messages.append({"role": "user", "content": f"{_FINAL_GATE_FAILURE_MESSAGE}\n\n{diagnostic}"})
                continue

            final_text = text or "(no response)"
            result_kind, result_detail, result_usage, result_cost = _classify_final_result(
                model=model,
                messages=messages,
                final_text=final_text,
                theorem_signature=_theorem_signature(proof_state.latest_proof_path),
                config=config,
            )
            total_usage.input_tokens += result_usage.input_tokens
            total_usage.output_tokens += result_usage.output_tokens
            total_cost += result_cost
            if result_usage.input_tokens or result_usage.output_tokens or result_cost:
                yield UsageUpdated(result_usage.input_tokens, result_usage.output_tokens, result_cost)
            final_transcript = transcript(turn)
            yield Finished("completed", final_text, turn, session_id, model,
                           total_usage, total_cost, final_transcript,
                           result_kind=result_kind, result_detail=result_detail)
            return

        def _exec_tool(tc):
            """Run one NON-spawn tool and return its result string. Pure (no yields), so
            it can run inline or on an E3 worker thread.

            C1: a failure is reported on BOTH channels. The returned string is what the
            MODEL reads (it needs it to recover) — but that string is invisible to the
            human, who saw only a "Running <tool>" chip while the agent quietly worked
            around a broken tool. `diagnostics.report` is the human's copy; the loop
            drains it in Phase 4. Reporting (not yielding) is what lets this stay a
            plain function callable from an E3 worker thread."""
            handler = tool_handlers.get(tc["name"])
            if handler:
                try:
                    r = handler(tc["args"])
                except Exception as e:  # noqa: BLE001 — a tool error is a result, not a crash
                    r = f"Error: tool '{tc['name']}' raised {type(e).__name__}: {e}"
                    diagnostics.report(
                        "step_error", "tool.raised",
                        f"{tc['name']} failed: {type(e).__name__}: {e}",
                        tool=tc["name"], turn=turn, path=(tc.get("args") or {}).get("path"),
                    )
            else:
                r = f"Error: unknown tool '{tc['name']}'"
                # A toolset misconfiguration (a profile naming a tool that isn't
                # registered, an MCP server that didn't come up). Today this failed
                # completely silently — the model saw the string and moved on.
                diagnostics.report(
                    "step_error", "tool.unknown",
                    f"The model called '{tc['name']}', which is not a registered tool.",
                    tool=tc["name"], turn=turn,
                )
            # Item 26: on a model-invoked lean_check, append the domain-scoped tactic
            # cascade for the mathematics in the checked file — once per domain per run.
            if tc["name"] == "lean_check":
                hint = _domain_cascade_for_check(tc.get("args") or {}, working_dir, surfaced_domains)
                if hint:
                    r = f"{r}\n\n{hint}"
            return r

        # Results by tool-call index, reassembled in the original order below so the
        # provider matches each tool_result to its call. A turn's spawns run CONCURRENTLY
        # (E2) and its independent read-only tools may too (E3); everything else stays
        # serial and in order.
        results_by_idx: dict[int, str] = {}
        spawn_specs = []      # (idx, tc, plan) — approved spawns with a real plan
        serial_calls = []     # (idx, tc) — approved non-spawn tools to execute
        # Calls that never ran because the human denied or cancelled the gate. Tracked
        # because `_meaning_events` decides "did this write succeed?" by sniffing the
        # result STRING (`_tool_result_ok` = non-empty and not starting with "error:")
        # — and the decline notice satisfies that, so a denied `write_file` emitted a
        # FileChanged for a write that never happened. The adapter then read the path
        # and recorded a code_step, attributing to the agent a write the user had just
        # refused. A refusal is knowledge the loop HAS; it must not be re-derived from
        # prose. (Pinned by tests/agent/test_gate.py, which this fixes.)
        refused_idx: set[int] = set()

        # Phase 1 — gate every call (D19; a two-way approval must run on the generator),
        # prepare + announce spawns, and bucket the rest for execution.
        for idx, tc in enumerate(tool_calls):
            approved = True
            if gate is not None and gate(tc["name"], tc["args"]):
                decision = yield ToolApprovalRequested(tc["name"], tc["args"])
                approved = decision in ("allow", "always_session")
            if not approved:
                refused_idx.add(idx)
                # E1: a CANCELLED gate (the human hit Stop while it was pending) is not
                # a declined one. Telling the model "the user declined" for a Stop made
                # it narrate around a judgement the user never expressed. Both outcomes
                # leave the tool unrun; only the explanation differs.
                if decision == "cancelled":
                    results_by_idx[idx] = (
                        f"This {tc['name']} call was cancelled because the user stopped the run "
                        "while the approval was pending. They did not decline the call itself. "
                        "The run is ending — summarize where you got to; do not start new work."
                    )
                else:
                    results_by_idx[idx] = (
                        f"The user declined to run this {tc['name']} call. Treat this as a redirect, "
                        "not a failure. Do NOT silently retry or jump to a different step. In your next "
                        "message, explain to the user what you were about to do and why, then ask how "
                        "they'd like to proceed — and wait for their reply before acting."
                    )
            elif tc["name"] == "spawn_subagent":
                try:
                    plan = subagents.prepare_spawn(tc["args"])
                except Exception as e:  # noqa: BLE001
                    results_by_idx[idx] = f"Error: tool 'spawn_subagent' raised {type(e).__name__}: {e}"
                    # No SubagentStarted is emitted on this path, so without a
                    # diagnostic a spawn that blew up leaves no trace at all — the UI
                    # shows a turn where the coordinator simply decided not to delegate.
                    diagnostics.report(
                        "step_error", "subagent.spawn_failed",
                        f"A sub-agent could not be started: {type(e).__name__}: {e}",
                        tool="spawn_subagent", turn=turn,
                    )
                    continue
                if isinstance(plan, str):
                    results_by_idx[idx] = plan  # refused → no child, no started event
                else:
                    # D1: announce every spawn as running BEFORE launching, so the UI shows
                    # the whole batch live from the outset.
                    # The full delegated task travels with the start event — the child's
                    # transcript (which holds it as the first user message) is not
                    # available until the child finishes.
                    _task = plan.child_messages[0]["content"] if plan.child_messages else ""
                    yield SubagentStarted(plan.result_id, plan.subagent_type,
                                          plan.description, _task)
                    spawn_specs.append((idx, tc, plan))
            else:
                serial_calls.append((idx, tc))

        # Phase 2 — non-spawn tools. E3: if a turn issues several INDEPENDENT read-only
        # tools, run them concurrently (a real win for F1's multi-modal search); otherwise
        # execute inline in order (any writer/checker/bash — a write→check pair must not
        # be reordered).
        if len(serial_calls) > 1 and all(tc["name"] in _PARALLEL_SAFE_TOOLS for _i, tc in serial_calls):
            out: dict[int, str] = {}
            threads = []
            for _i, _tc in serial_calls:
                def _work(i=_i, t=_tc, ctx=contextvars.copy_context()):
                    out[i] = ctx.run(_exec_tool, t)
                th = threading.Thread(target=_work, daemon=True)
                th.start()
                threads.append(th)
            for th in threads:
                th.join()
            results_by_idx.update(out)
        else:
            for idx, tc in serial_calls:
                results_by_idx[idx] = _exec_tool(tc)

        # Phase 3 — E2: run this turn's spawns concurrently, streaming all their live
        # events up as they arrive (the coordinator's model context stays isolated —
        # these SubagentProgress events never enter `messages`; only the render does).
        if spawn_specs:
            renders = yield from subagents.run_children_concurrently([p for _i, _tc, p in spawn_specs])
            for idx, _tc, plan in spawn_specs:
                results_by_idx[idx] = renders.get(plan.result_id) or (
                    f"Error: subagent '{plan.subagent_type}' produced no result."
                )

        # Phase 4 — emit each tool's downstream events + assemble tool_results IN ORDER.
        # First surface anything the tools reported for the HUMAN (C1/C4). Drained here,
        # once, rather than per call: a diagnostic carries its own anchor (`tool`,
        # `turn`, `path`), so the adapter places it — its position in the raw stream is
        # cosmetic. Emitted BEFORE the tool_results so a failure reads ahead of the
        # model's reaction to it.
        for diag in diagnostics.drain():
            yield diag

        tool_results = []
        for idx, tc in enumerate(tool_calls):
            result = results_by_idx.get(idx, f"Error: tool '{tc['name']}' produced no result")
            preview = result[:200] + "..." if len(result) > 200 else result
            yield ToolResulted(tc["name"], result, preview)
            # A refused call had no effect, so it has no meaning events and must not
            # move the proof state — otherwise a denied write marks the proof as
            # freshly written and due a check.
            if idx not in refused_idx:
                for ev in _meaning_events(tc["name"], tc["args"], result):
                    yield ev
                proof_state.note_tool_result(tc["name"], tc["args"], result)
            tool_result = {"type": "tool_result", "tool_name": tc["name"], "content": result}
            if tc["id"]:
                tool_result["tool_use_id"] = tc["id"]
                tool_result["tool_call_id"] = tc["id"]
            tool_results.append(tool_result)

        # Each spawn produced a typed result the tool_result string can't carry (the child
        # transcript, the result id), surfaced as a SubagentFinished so the adapter stores
        # the transcript + keeps the audit link.
        #
        # Concurrent spawns now emit theirs as each child finishes (inside
        # `run_children_concurrently`), so this normally finds an empty collector. It
        # stays as the sweep for the paths that do NOT go through that helper — a
        # `spawn_subagent` called directly via the registered tool — and for any result
        # recorded after the last child's completion message was processed.
        if spawn_specs:
            for child_result in subagents.drain_results():
                yield child_result.to_event()

        messages.append({"role": "user", "content": tool_results})
