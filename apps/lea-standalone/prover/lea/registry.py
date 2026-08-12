"""Declarative tool registry — the single place a tool's schema and handler live.

Every tool is one `Tool` record (model-facing schema + a `dict[args] -> str`
handler). Built-in tools register themselves when `lea.tools` is imported;
custom tools register from user modules named in `agent.tool_modules`; MCP tools
(a later step) will register here too. The agent loop never imports tools
directly — it asks `build_toolset(selected)` for exactly the tools config wants.

Public API for custom tools:

    from lea.registry import tool

    @tool(name="sympy", description="...", input_schema={...})
    def sympy(args: dict) -> str:
        ...
"""

import contextvars
import importlib
from dataclasses import dataclass
from typing import Callable

from .errors import ToolError

Handler = Callable[[dict], str]


@dataclass(frozen=True)
class Tool:
    """A registered tool: its model-facing schema and its handler.

    `schema` is the JSON object sent to the model (name/description/input_schema).
    `handler` takes the raw arguments dict and returns a string result.

    `opt_in` (item 18) marks a tool that the unfiltered (`selected is None`)
    toolset must NOT include — a config has to name it explicitly. `spawn_subagent`
    is opt-in: it stays off every existing run (which pass `tools=None`) so
    subagents land dark, and a subagent's own `tools=None` toolset can never
    contain it — the second recursion guard, for free.
    """

    name: str
    schema: dict
    handler: Handler
    opt_in: bool = False
    # A tool whose schema depends on run-time state supplies a factory instead of relying
    # on the frozen `schema` (v2.5 B1). `spawn_subagent` is the case: the roles a
    # coordinator may delegate to are discovered from disk (and, once user-authored roles
    # land, from the database), so a schema fixed at import time can only ever advertise
    # the roles that existed when the module was first imported. `schema` stays as the
    # static fallback, so every other tool is unaffected.
    schema_factory: Callable[[], dict] | None = None


# The GLOBAL base layer: name -> Tool, plus registration order so an unfiltered
# toolset is deterministic (and reproduces today's TOOLS_SCHEMA order for the
# built-ins). Everything registered at *import time* — the built-ins, user
# `tool_modules` — lives here and is shared, immutable, and process-wide.
REGISTRY: dict[str, Tool] = {}
_ORDER: list[str] = []

# The per-activation OVERLAY (item 27). Tools registered *during* a run and scoped
# to it — today only MCP tools — go here instead of the global, so two concurrent
# MCP-enabled activations can't corrupt each other's toolsets (the old failure:
# activation A's `stop()` unregistered activation B's tools by name out of the one
# shared dict). `build_toolset` reads global + the active overlay; the overlay is a
# fresh dict per activation (agent.run_events pushes/pops it), dropped on exit — so a
# run's dynamic tools simply cease to exist when it ends, no cross-run `unregister`.
#
# Overlay tools are a **dict** (insertion-ordered) so it doubles as membership + order
# for overlay-only names. Default None = "no activation scope" (import time, the CLI,
# standalone tests, the mcp unit test) → registration falls through to the global, so
# every pre-item-27 path behaves byte-identically.
_scope: contextvars.ContextVar[dict[str, Tool] | None] = contextvars.ContextVar(
    "lea_registry_scope", default=None
)


def push_scope():
    """Open a fresh per-activation tool overlay; returns a reset token. Called once at
    the top of `run_events` so a run's dynamic (MCP) tools land in an isolated layer."""
    return _scope.set({})


def pop_scope(token) -> None:
    """Close the overlay opened by `push_scope`, dropping every tool registered into it."""
    import contextlib
    with contextlib.suppress(ValueError):
        _scope.reset(token)


def _active_scope() -> dict[str, Tool] | None:
    return _scope.get()


def get_tool(name: str) -> Tool | None:
    """Resolve a name against the effective registry: the active overlay wins over the
    global base (though a scoped registration can never shadow a global name — see
    `register`), else the global."""
    scope = _active_scope()
    if scope is not None and name in scope:
        return scope[name]
    return REGISTRY.get(name)


def is_registered(name: str) -> bool:
    """True if `name` resolves in the effective registry (overlay or global)."""
    return get_tool(name) is not None


def _all_names() -> set[str]:
    scope = _active_scope()
    return set(REGISTRY) | (set(scope) if scope else set())


def register(tool: Tool, *, scoped: bool = False) -> Tool:
    """Add a Tool to the registry. Raises ToolError on a duplicate name (checked against
    the *effective* registry, so a dynamic tool can't silently shadow a built-in).

    `scoped=True` targets the active per-activation overlay when one is open (MCP tools,
    so they're isolated per run and vanish on run exit); with no overlay open it falls
    through to the global, preserving the pre-item-27 behavior for standalone MCP use.
    Import-time registrations (`@tool`, `tool_modules`) leave `scoped=False` and always
    land in the global — they're process-wide and import-cached, so scoping them would
    make them vanish after the first run that imported them.
    """
    if is_registered(tool.name):
        raise ToolError(f"tool {tool.name!r} is already registered")
    scope = _active_scope()
    if scoped and scope is not None:
        scope[tool.name] = tool
    else:
        REGISTRY[tool.name] = tool
        _ORDER.append(tool.name)
    return tool


def unregister(name: str) -> None:
    """Remove a tool from the registry (no-op if absent). Removes from the active overlay
    when the tool lives there (the MCP-during-a-run case), else from the global.

    Used to tear down dynamically-registered tools (e.g. MCP tools when their manager
    stops) so a later run can re-register them without a duplicate clash. With the item-27
    overlay, popping the whole scope on run exit already does this — but MCP's explicit
    `stop()` still calls here, and now it only ever touches *this* run's overlay.
    """
    scope = _active_scope()
    if scope is not None and name in scope:
        del scope[name]
        return
    REGISTRY.pop(name, None)
    if name in _ORDER:
        _ORDER.remove(name)


def tool(*, name: str, description: str, input_schema: dict, opt_in: bool = False,
         schema_factory: Callable[[], dict] | None = None):
    """Decorator: register a `dict[args] -> str` function as a Tool.

    The function becomes the handler; the schema is assembled from the arguments.
    `opt_in=True` keeps the tool out of the default (`selected is None`) toolset.
    `schema_factory` (B1) rebuilds the schema on every `build_toolset`, for a tool whose
    shape depends on run-time state rather than on what existed at import.
    """

    schema = {"name": name, "description": description, "input_schema": input_schema}

    def decorator(fn: Handler) -> Handler:
        register(Tool(name=name, schema=schema, handler=fn, opt_in=opt_in,
                      schema_factory=schema_factory))
        return fn

    return decorator


def import_tool_modules(modules: list[str]) -> None:
    """Import each module so its @tool/register side effects run.

    Used for `agent.tool_modules` — a user points at Python modules that register
    custom tools. Raises ToolError if a module can't be imported.
    """
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as e:  # ImportError, or an error raised at import time
            raise ToolError(f"could not import tool module {name!r}: {e}") from e


def build_toolset(selected: list[str] | None,
                  extra: list[str] | None = None) -> tuple[list[dict], dict[str, Handler]]:
    """Resolve a config selection into what the loop needs: (schemas, handlers).

    `selected is None` → every registered tool EXCEPT opt-in ones (item 18), in
    registration order (the default; reproduces today's behavior — the built-ins
    are all non-opt-in). A list → exactly those tools, in that order (so the list
    both filters and orders), opt-in tools included when named explicitly. An
    unknown name raises ToolError.

    Reads the *effective* registry (item 27): the global base plus this activation's
    tool overlay, if one is open. The unfiltered order is the global registration order
    followed by the overlay's insertion order, so a run's MCP tools append after the
    built-ins deterministically.
    """
    if selected is None:
        scope = _active_scope()
        names = [n for n in _ORDER if not REGISTRY[n].opt_in]
        if scope:
            names += [n for n, t in scope.items() if not t.opt_in]
        # `extra` names OPT-IN tools to add on top of the default set (v2.5). It exists so
        # a caller can say "everything, plus spawn_subagent" WITHOUT freezing the list:
        # the adapter used to express that by resolving `build_toolset(None)` in its own
        # process and passing the result as an explicit allowlist — a snapshot taken
        # before MCP and HTTP tools register inside the run, which silently excluded every
        # one of them from every UI run.
        names += [n for n in (extra or []) if n not in names]
    else:
        names = selected
    schemas: list[dict] = []
    handlers: dict[str, Handler] = {}
    for name in names:
        t = get_tool(name)
        if t is None:
            raise ToolError(
                f"unknown tool {name!r}; registered tools: {', '.join(sorted(_all_names()))}"
            )
        # B1: a dynamic tool rebuilds its schema per toolset build, so what the model is
        # offered reflects the roles that exist NOW, not at import time.
        schemas.append(t.schema_factory() if t.schema_factory is not None else t.schema)
        handlers[name] = t.handler
    return schemas, handlers
