"""The channel a tool uses to tell the HUMAN something went wrong (v2.4).

A tool handler is `dict -> str`, and that string is the `tool_result` the MODEL
reads. So a tool that fails has no way to reach the person watching: `lean_check`
silently falling back from the LSP daemon to a cold `lake env lean` (~0.2s -> ~88s)
looked, from the UI, like the agent thinking for a minute and a half.

`report()` records a `Diagnostic` into a per-activation collector; the agent loop
drains it right after each tool call and yields the events up (the same shape
`subagents._results` uses for child results). Three consequences worth keeping:

  * **Handler signatures don't change.** Any tool — built-in, user `tool_modules`,
    or MCP — can report without the loop knowing anything about it. This is the
    cheap half of "tools declare their own effects"; the typed-result refactor is
    the other half.
  * **Thread-safe by construction.** `contextvars.copy_context()` copies the
    *mapping*, not the values, so a tool running on an E2/E3 worker thread appends
    to the same list object the coordinator drains. `list.append` is atomic under
    the GIL.
  * **A no-op outside an activation.** With no scope open (`interface.check()`, the
    CLI, a unit test) the collector is None and `report()` does nothing, so nothing
    has to be conditionally wired.
"""

from __future__ import annotations

import contextvars

from .events import Diagnostic

# The per-activation collector. `None` = no live activation -> report() is a no-op.
_collector: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "lea_diagnostics", default=None
)
# Codes already reported in THIS activation, for `once=True`. Kept for the whole
# activation (not just since the last drain) so a degraded condition rechecked on
# every turn — the cold-compile fallback — is announced once, not forty times.
_seen: contextvars.ContextVar[set | None] = contextvars.ContextVar(
    "lea_diagnostics_seen", default=None
)

SEVERITIES = ("fatal", "step_error", "degraded", "notice")


def begin_scope():
    """Open a fresh collector for this activation; returns (collector_token, seen_token)."""
    return _collector.set([]), _seen.set(set())


def end_scope(tokens) -> None:
    import contextlib

    collector_token, seen_token = tokens
    with contextlib.suppress(ValueError):
        _collector.reset(collector_token)
    with contextlib.suppress(ValueError):
        _seen.reset(seen_token)


def report(
    severity: str,
    code: str,
    message: str,
    *,
    source: str = "tool",
    remedy: str | None = None,
    once: bool = False,
    **context,
) -> None:
    """Record a human-facing diagnostic from inside a tool.

    `once=True` reports only the first occurrence of `code` in this activation —
    for an ongoing degraded condition, where every later occurrence is the same
    fact rather than new information. No-op when no activation scope is open.
    """
    collector = _collector.get()
    if collector is None:
        return
    if once:
        seen = _seen.get()
        if seen is not None:
            if code in seen:
                return
            seen.add(code)
    collector.append(
        Diagnostic(
            severity=severity if severity in SEVERITIES else "notice",
            code=code,
            message=message,
            source=source,
            remedy=remedy,
            context={k: v for k, v in context.items() if v is not None},
        )
    )


def drain() -> list[Diagnostic]:
    """Return and clear everything reported since the last drain (the agent loop
    calls this after each tool call)."""
    collector = _collector.get()
    if not collector:
        return []
    out = list(collector)
    collector.clear()
    return out
