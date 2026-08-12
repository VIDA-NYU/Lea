"""Per-activation run context (item 8, v2.3 concurrency).

``ContextVar``s scoping one Lea activation's identity and nesting:

  * ``working_dir`` — the directory an activation reads/writes proofs in. Tools
    that touch the filesystem (today ``bash``) read it here instead of inheriting
    the one process-global cwd.
  * ``run_key``     — a stable key naming the activation, for lock/scratch keys
    downstream (items 9/11). Defaults to the session id.
  * ``candidate_dir`` — the isolated dir a *subagent* writes to before its parent
    promotes it (item 11). ``None`` until that lands; carried now so the seam
    exists.
  * ``depth``       — activation nesting depth (item 18). 0 for a top-level run;
    a subagent spawned by ``spawn_subagent`` runs at ``parent depth + 1``. The
    spawn tool refuses at ``depth >= max_depth`` (default 1), so a child cannot
    spawn — one of the two independent recursion guards (the other: ``spawn_subagent``
    is absent from a child's toolset).
  * ``config``      — the active activation's :class:`LeaConfig` (item 18), so a
    tool that starts a *child* activation (``spawn_subagent``) can derive the
    child's config (same model/provider, scoped tools) without the handler — which
    only receives ``args`` — being handed it explicitly.

**Why ContextVars, not thread-locals or an argument threaded everywhere.** They
are per-thread *and* per-async-task, and a child task/thread launched via
``contextvars.copy_context()`` inherits the parent's values. That inheritance is
the primitive the subagent architecture stands on — a child sees the parent's
``run_key``/``candidate_dir`` without every call passing them along. And because
each run drives its generator in its own thread, two concurrent runs never see
each other's ``working_dir`` — which is exactly the ``bash`` bug today:
``subprocess.run(shell=True)`` with no ``cwd=`` inherits the single process-global
cwd, so under concurrent runs one run's shell command lands in another's tree.

**No process-global mutation.** ``os.chdir`` is never called. The working dir
lives only in the ContextVar and is passed explicitly to ``subprocess`` — so
setting it for one activation can't move the cwd out from under another.

Reads return ``None`` when no context is established (a standalone/test call, or
the loose path that never set one), so callers get today's behavior unchanged:
``cwd=None`` tells ``subprocess`` to use the inherited cwd.
"""

from __future__ import annotations

import contextlib
import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a runtime import cycle; only needed for the type hint
    from .config import LeaConfig

_working_dir: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lea_working_dir", default=None
)
_run_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lea_run_key", default=None
)
_candidate_dir: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lea_candidate_dir", default=None
)
_depth: contextvars.ContextVar[int] = contextvars.ContextVar("lea_depth", default=0)
_config: contextvars.ContextVar["LeaConfig | None"] = contextvars.ContextVar(
    "lea_config", default=None
)
# The active activation's cooperative-stop predicate (D18). Carried here so a tool that
# starts a *child* activation (spawn_subagent) can compose it INTO the child's own stop
# — a coordinator stop then also halts a running child (E1/D2), instead of the child
# blocking the coordinator's stop until it finishes on its own.
_should_stop: contextvars.ContextVar["object | None"] = contextvars.ContextVar(
    "lea_should_stop", default=None
)


def current_working_dir() -> str | None:
    """The active run's working directory, or ``None`` outside any run context."""
    return _working_dir.get()


def current_run_key() -> str | None:
    """The active run's stable key, or ``None`` outside any run context."""
    return _run_key.get()


def current_candidate_dir() -> str | None:
    """The active run's isolated candidate dir (item 11), or ``None``."""
    return _candidate_dir.get()


def current_depth() -> int:
    """The active activation's nesting depth (item 18): 0 top-level, +1 per
    subagent. ``0`` outside any run context."""
    return _depth.get()


def current_config() -> "LeaConfig | None":
    """The active activation's :class:`LeaConfig` (item 18), or ``None`` outside a
    run context. ``spawn_subagent`` reads it to derive the child's config."""
    return _config.get()


def current_should_stop():
    """The active activation's cooperative-stop predicate (D18), or ``None``.
    ``spawn_subagent`` composes it into a child's stop so a coordinator stop cascades."""
    return _should_stop.get()


@contextlib.contextmanager
def run_context(
    *,
    working_dir: str | None = None,
    run_key: str | None = None,
    candidate_dir: str | None = None,
    depth: int = 0,
    config: "LeaConfig | None" = None,
    should_stop=None,
):
    """Establish the run context for the duration of the ``with`` block.

    Set at the activation boundary (``agent.run_events``). Tokens are reset in a
    ``finally`` so a pooled/reused thread returns to the default afterwards rather
    than leaking one run's ``working_dir`` into the next call on that thread.
    """
    tokens = (
        _working_dir.set(working_dir),
        _run_key.set(run_key),
        _candidate_dir.set(candidate_dir),
        _depth.set(depth),
        _config.set(config),
        _should_stop.set(should_stop),
    )
    try:
        yield
    finally:
        # Reset in reverse; each reset runs in the same context .set() ran in
        # (the activation drives its generator on one thread), so no cross-context
        # reset. Guarded so a teardown surprise can't mask the real exception.
        wd, rk, cd, dp, cf, ss = tokens
        with contextlib.suppress(ValueError):
            _should_stop.reset(ss)
        with contextlib.suppress(ValueError):
            _config.reset(cf)
        with contextlib.suppress(ValueError):
            _depth.reset(dp)
        with contextlib.suppress(ValueError):
            _candidate_dir.reset(cd)
        with contextlib.suppress(ValueError):
            _run_key.reset(rk)
        with contextlib.suppress(ValueError):
            _working_dir.reset(wd)
