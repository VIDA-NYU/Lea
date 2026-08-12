"""Context compaction — the condenser (G1). Prune → summarize.

A long proving run's `messages` list is dominated by superseded tool output:
full `lean_check` error dumps, `read_file` bodies, `search_mathlib` hit lists —
each thousands of tokens, most made irrelevant by a later edit + recheck. Left
unchecked the coordinator's context grows without bound (the 15M-token run that
motivated this). The condenser keeps the model-facing window bounded WITHOUT
losing the thread, in two stages modelled on OpenHands'
`pipeline([observation_masking, llm_summarizing])`:

  1. PRUNE (deterministic, no LLM) — mask the *content* of superseded read-only
     tool results outside a recent window with a short placeholder. Cheap, keeps
     the message shape intact (the tool_result part and its id survive), and
     reclaims the bulk of the tokens. Runs whenever compaction triggers.

  2. SUMMARIZE (one LLM call) — only if pruning did not get back under the
     threshold: fold the older MIDDLE of the conversation into a single summary
     message, keeping the leading user context (the immutable goal) and the most
     recent turns verbatim. A CODE_STATE / TESTS / CHANGES summary (Lean-tuned)
     preserves what was tried and where the proof stands.

Message-shape safety is the load-bearing invariant. Every assistant tool_call
must keep its matching tool_result — LiteLLM turns each result into a
`role:"tool"` message keyed by id, and an orphan errors the provider. So the
summarizer only ever removes COMPLETE turns: the head is the leading all-user
context (it holds no tool calls), the tail always starts at an assistant
message, and the forgotten span between them is whole assistant+result pairs.
The condenser is copy-on-write — it never mutates the caller's message dicts.

This module is a lower layer than agent.py (agent imports it); it depends only
on `providers` (for the one summary stream) and `config`, never the loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .config import LeaConfig
from .providers import stream, TextDelta, Done, Usage

# Read-only tools whose results go stale the moment a later step supersedes them —
# a `lean_check` dump is dead weight once the file was edited and rechecked; a
# `read_file` / `search_mathlib` result, once acted on. These are safe to mask
# outside the recent window. Writers/`bash` are NOT here: their output is more
# likely load-bearing and is rarely the token hog anyway.
_PRUNABLE_TOOLS = frozenset(
    {"lean_check", "read_file", "search_mathlib", "suggest_imports"}
)

# A masked result must still say enough that the model knows something was there
# and why it's gone — but stay tiny. Only content longer than this is worth masking.
_MIN_PRUNE_CHARS = 200

# Rough char-per-token gauge for the INTERNAL size estimate. The compaction
# *trigger* uses the provider's real input-token count; this heuristic only sizes
# the message text so we can tell whether pruning alone got us under the bar
# before paying for a summary call.
_CHARS_PER_TOKEN = 4

# Per-event truncation when flattening the forgotten span for the summarizer — long
# enough to carry a lemma signature or the gist of an error, short enough that a big
# forgotten region still fits one prompt.
_SUMMARY_PART_CHARS = 2000


def _placeholder(tool_name: str | None) -> str:
    label = tool_name or "tool"
    return f"[Earlier {label} output pruned to save context — superseded by later steps.]"


@dataclass(frozen=True)
class CondenseResult:
    """Outcome of one `condense` call. `messages` is the new (copy-on-write) history to
    send; `changed` is True iff anything was pruned or summarized. `before_tokens` /
    `after_tokens` are estimated input-token sizes for the UI marker; `pruned` counts
    masked results; `summarized` is 1 if the middle was folded, else 0. `usage`/`cost`
    account only for the summary LLM call (zero when pruning sufficed)."""

    messages: list
    changed: bool
    before_tokens: int
    after_tokens: int
    pruned: int
    summarized: int
    usage: Usage
    cost: float


def _text_size(messages: list) -> int:
    """Total characters of model-facing text across the history (rough gauge)."""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for p in content:
                if not isinstance(p, dict):
                    continue
                t = p.get("type")
                if t == "text":
                    total += len(str(p.get("text") or ""))
                elif t == "tool_call":
                    total += len(p.get("name") or "") + len(json.dumps(p.get("args") or {}))
                elif t == "tool_result":
                    total += len(str(p.get("content") or ""))
    return total


def estimate_tokens(messages: list) -> int:
    """A cheap char/4 token estimate for the message text (excludes system + tools)."""
    return _text_size(messages) // _CHARS_PER_TOKEN


def referenced_files(messages: list, limit: int = 20) -> list[str]:
    """Distinct file paths still referenced by tool calls in `messages`, first-seen order.

    After a compaction this is "what the model still has in view" — the files its kept
    tool calls (read_file / write_file / edit_file / lean_check) touch. The manual
    `/compact` surface shows it as a Claude-Code-style 'still referenced' list, so the
    user sees what survived, not just a token delta."""
    seen: list[str] = []
    for m in messages:
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue
        for p in m["content"]:
            if not isinstance(p, dict) or p.get("type") != "tool_call":
                continue
            path = (p.get("args") or {}).get("path")
            if isinstance(path, str) and path and path not in seen:
                seen.append(path)
                if len(seen) >= limit:
                    return seen
    return seen


def compaction_trigger(config: LeaConfig) -> int:
    """The input-token count at or above which compaction fires. 0 → disabled."""
    if not config.context_token_limit or config.context_token_limit <= 0:
        return 0
    return int(config.context_token_limit * config.compaction_threshold)


def should_compact(last_input_tokens: int, config: LeaConfig) -> bool:
    """True when the last turn's real input-token count crossed the trigger."""
    trigger = compaction_trigger(config)
    return bool(trigger) and last_input_tokens >= trigger


def prune_superseded(
    messages: list,
    keep_recent_results: int,
    prunable: frozenset = _PRUNABLE_TOOLS,
    min_chars: int = _MIN_PRUNE_CHARS,
) -> tuple[list, int]:
    """Mask superseded read-only tool results outside the recent window (copy-on-write).

    The last `keep_recent_results` prunable results are kept verbatim (the model is
    actively working the newest errors); earlier ones with substantial content get
    their `content` replaced by a short placeholder. The tool_result part and its id
    are preserved, so no `role:"tool"` message is orphaned. Returns (new_messages, n)."""
    # Locate every prunable tool_result, in order.
    positions: list[tuple[int, int]] = []
    for mi, m in enumerate(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for pi, p in enumerate(m["content"]):
                if isinstance(p, dict) and p.get("type") == "tool_result" \
                        and p.get("tool_name") in prunable:
                    positions.append((mi, pi))
    if not positions:
        return messages, 0
    mask_set = set(positions if keep_recent_results <= 0 else positions[:-keep_recent_results])
    if not mask_set:
        return messages, 0

    masked_msgs = {mi for mi, _pi in mask_set}
    pruned = 0
    out: list = []
    for mi, m in enumerate(messages):
        if mi not in masked_msgs:
            out.append(m)
            continue
        new_content = []
        for pi, p in enumerate(m["content"]):
            if (mi, pi) in mask_set and isinstance(p, dict) \
                    and len(str(p.get("content") or "")) > min_chars:
                np = dict(p)
                np["content"] = _placeholder(p.get("tool_name"))
                new_content.append(np)
                pruned += 1
            else:
                new_content.append(p)
        nm = dict(m)
        nm["content"] = new_content
        out.append(nm)
    return out, pruned


_SUMMARY_SYSTEM = """\
You are maintaining a context-aware state summary for a Lean 4 theorem-proving \
agent whose earlier work is being condensed to save context. You are given the \
goal and a log of the agent's earlier events (its narration, the tools it called, \
and truncated tool results). Produce a compact, information-dense summary the agent \
can continue from — not a narrative. Preserve exact identifiers.

Track only the sections that apply:

GOAL: the theorem/statement being proved, verbatim signature if seen.
PROOF_STATE: the current .lean file path(s) and the closest-to-working proof state.
TRIED: approaches/tactics attempted and their outcome (what worked, what failed and why).
ERRORS: the most recent blocking Lean errors, with the exact error text/line if known.
LEMMAS: Mathlib lemma names + signatures found useful (and dead ends to avoid re-searching).
NEXT: the single most promising next step.

Rules: keep exact lemma names, file paths, tactic names, and error messages — these are \
load-bearing. Be concise; drop pleasantries and superseded detail. Output only the summary."""


def _flatten_for_summary(messages: list, max_part: int = _SUMMARY_PART_CHARS) -> str:
    """Render head+forgotten messages to a plain text log for the summarizer."""
    lines: list[str] = []
    for m in messages:
        role = (m.get("role") or "").upper()
        content = m.get("content")
        if isinstance(content, str):
            if content.strip():
                lines.append(f"{role}: {content[:max_part]}")
        elif isinstance(content, list):
            for p in content:
                if not isinstance(p, dict):
                    continue
                t = p.get("type")
                if t == "text" and (p.get("text") or "").strip():
                    lines.append(f"{role}: {str(p['text'])[:max_part]}")
                elif t == "tool_call":
                    args = json.dumps(p.get("args") or {}, ensure_ascii=False)
                    lines.append(f"TOOL_CALL {p.get('name')}: {args[:max_part]}")
                elif t == "tool_result":
                    lines.append(
                        f"TOOL_RESULT {p.get('tool_name') or ''}: "
                        f"{str(p.get('content') or '')[:max_part]}"
                    )
    return "\n".join(lines)


_SUMMARY_WRAPPER = (
    "The earlier part of this session has been condensed to save context. Here is a "
    "summary of the work done so far — treat it as established context and continue "
    "from it:\n\n<SESSION SUMMARY>\n{summary}\n</SESSION SUMMARY>"
)


def _summarize(model: str, forgotten_log: str, config: LeaConfig) -> tuple[str, Usage, float]:
    """One tool-less LLM call: turn the forgotten log into a state summary. Best-effort —
    any failure returns an empty summary so compaction degrades to prune-only."""
    text = ""
    usage = Usage()
    cost = 0.0
    history = [{"role": "user", "content":
                f"Earlier events to summarize:\n\n{forgotten_log}\n\nNow write the summary."}]
    try:
        for event in stream(model, _SUMMARY_SYSTEM, history, [], config.model_kwargs,
                            streaming=config.stream):
            if isinstance(event, TextDelta):
                text += event.text
            elif isinstance(event, Done):
                usage.input_tokens += event.usage.input_tokens
                usage.output_tokens += event.usage.output_tokens
                cost += event.cost
    except Exception:  # noqa: BLE001 — a best-effort terminal path, never fatal
        return "", Usage(), 0.0
    return text.strip(), usage, cost


def summarize_middle(
    messages: list,
    keep_recent_turns: int,
    model: str,
    config: LeaConfig,
) -> tuple[list, int, Usage, float]:
    """Fold the older middle into one summary message (copy-on-write).

    Head = every leading message before the first assistant turn (the goal + any
    prepended context; holds no tool calls). Tail = the last `keep_recent_turns`
    assistant turns and everything after, always starting AT an assistant message so
    no tool_result is orphaned. The forgotten span between them — whole assistant+
    result pairs — becomes a single summary user message. Returns
    (new_messages, summarized, usage, cost); summarized is 0 (unchanged) when there is
    nothing safe to fold or the summary call yielded nothing."""
    assistant_idxs = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
    if len(assistant_idxs) <= keep_recent_turns:
        return messages, 0, Usage(), 0.0
    first_assistant = assistant_idxs[0]
    tail_start = assistant_idxs[-keep_recent_turns]
    # Need a non-trivial middle to be worth a summary call (≥ one full assistant+result pair).
    if tail_start - first_assistant < 2:
        return messages, 0, Usage(), 0.0

    head = messages[:first_assistant]
    forgotten = messages[first_assistant:tail_start]
    tail = messages[tail_start:]

    summary, usage, cost = _summarize(model, _flatten_for_summary(head + forgotten), config)
    if not summary:
        return messages, 0, usage, cost

    block = _SUMMARY_WRAPPER.format(summary=summary)
    # Prefer folding the summary into the head's trailing user-string message (the goal),
    # so no `user, user` seam is introduced before the assistant tail. Fall back to a
    # standalone user message only if the head has no such message to extend.
    if head and head[-1].get("role") == "user" and isinstance(head[-1].get("content"), str):
        merged_head = list(head)
        merged_head[-1] = {"role": "user", "content": f"{head[-1]['content']}\n\n{block}"}
        return merged_head + tail, 1, usage, cost
    return head + [{"role": "user", "content": block}] + tail, 1, usage, cost


def condense(
    messages: list,
    config: LeaConfig,
    *,
    model: str,
    last_input_tokens: int,
    force: bool = False,
) -> CondenseResult:
    """Run the condenser once: prune, then summarize only if still over the trigger.

    `last_input_tokens` is the provider's real input-token count for the last turn (the
    trigger signal). Pruning's effect on real tokens is estimated by applying the
    heuristic char-ratio reduction to that real count, so the decision to also pay for a
    summary stays grounded in actual context size rather than the rough char gauge.

    `force` (the manual `/compact` path, G3) skips the threshold gate and always attempts
    the summary fold — the user asked to compact now, so shrink as much as is safe. The
    summarize stage still self-guards on length, so a short session just prunes."""
    before_est = estimate_tokens(messages)
    # System prompt + tool schemas are in the real count but not our text gauge — carry
    # that constant overhead through so the post-prune real estimate stays honest.
    overhead = max(0, last_input_tokens - before_est)

    work, pruned = prune_superseded(messages, config.compaction_keep_recent_results)
    after_prune_real = estimate_tokens(work) + overhead

    summarized = 0
    usage = Usage()
    cost = 0.0
    trigger = compaction_trigger(config)
    if force or (trigger and after_prune_real >= trigger):
        work, summarized, s_usage, s_cost = summarize_middle(
            work, config.compaction_keep_recent_turns, model, config)
        usage.input_tokens += s_usage.input_tokens
        usage.output_tokens += s_usage.output_tokens
        cost += s_cost

    after_est_real = estimate_tokens(work) + overhead
    changed = pruned > 0 or summarized > 0
    return CondenseResult(
        messages=work,
        changed=changed,
        before_tokens=last_input_tokens,
        after_tokens=after_est_real,
        pruned=pruned,
        summarized=summarized,
        usage=usage,
        cost=cost,
    )
