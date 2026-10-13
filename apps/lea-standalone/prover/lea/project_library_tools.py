"""Opt-in, host-mediated project tools. No adapter or database dependencies."""
from dataclasses import dataclass
from .registry import Tool, register

TOOL_NAMES = ("search_project_library", "request_project_dependency")


@dataclass(frozen=True)
class ProjectLibraryRequested:
    tool: str
    arguments: dict
    invocation_key: str


@dataclass(frozen=True)
class ProjectLibraryAck:
    result: dict | None = None
    error: str | None = None


def _unsupported(_):
    return "Error: this tool requires a project-library host."


register(Tool(TOOL_NAMES[0], {
    "name": TOOL_NAMES[0],
    "description": "Search this project's mathematics before proving a supporting result. Returns full Lean types, imports, visibility and current verification evidence. A Claim definition or theorem assuming a claim does not prove that claim. Use existing search_mathlib afterward.",
    "input_schema": {"type": "object", "properties": {
        "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "offset": {"type": "integer", "minimum": 0}}, "required": ["query"], "additionalProperties": False},
}, _unsupported, opt_in=True))
register(Tool(TOOL_NAMES[1], {
    "name": TOOL_NAMES[1],
    "description": "Reuse or reserve supporting mathematics under a canonical identity. Use inline for a short helper, deferred for substantial/cited results. The host assigns the path/name/skeleton. Preserve its signature and defining body. Deferred work pauses at a saved checkpoint. Existing/blocked results must not be independently reproved. A citation key is metadata, never a proof.",
    "input_schema": {"type": "object", "properties": {
        "mode": {"type": "string", "enum": ["inline", "deferred"]},
        "specification": {"type": "object", "properties": {
            "name": {"type": "string"}, "kind": {"type": "string", "enum": ["theorem", "definition"]},
            "statement": {"type": "string"},
            "signature": {"type": "string", "description": "Lean binders and result type, excluding declaration name and := proof. Include implicit context explicitly. Empty only for an unresolved source obligation."},
            "body": {"type": "string", "description": "Pinned body for a shared definition; omit for theorems."},
            "imports": {"type": "array", "items": {"type": "string"}},
            "sources": {"type": "array", "items": {"type": "object"}},
            "citation": {"type": "object", "properties": {"key": {"type": "string"}, "locator": {"type": "string"}}},
            "notes": {"type": "string"}}, "required": ["name", "kind", "statement", "signature", "imports", "sources"], "additionalProperties": False}},
        "required": ["mode", "specification"], "additionalProperties": False},
}, _unsupported, opt_in=True))
