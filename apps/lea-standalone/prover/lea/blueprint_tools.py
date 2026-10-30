"""Opt-in, host-mediated blueprint tools. No adapter or database dependencies.

The agent never writes blueprint markdown (docs/PLAN-blueprint-tools.md §2.3). It
calls these tools; the loop yields `BlueprintRequested` and the host (the adapter's
`blueprint_host`) validates, assigns declaration names, renders `.lea/blueprint.md`
and answers with `BlueprintAck`. The model sees `json.dumps(result)` or
`"Error: <error>"`.
"""
from dataclasses import dataclass
from .registry import Tool, register

TOOL_NAMES = ("blueprint_read", "blueprint_upsert", "blueprint_remove")


@dataclass(frozen=True)
class BlueprintRequested:
    tool: str
    arguments: dict
    invocation_key: str


@dataclass(frozen=True)
class BlueprintAck:
    result: dict | None = None
    error: str | None = None


def _unsupported(_):
    return "Error: this tool requires a blueprint host."


register(Tool(TOOL_NAMES[0], {
    "name": TOOL_NAMES[0],
    "description": "Read the project blueprint: every node with its derived status, which nodes are ready to prove, and what blocks the rest. Call this instead of reading .lea/blueprint.md.",
    "input_schema": {"type": "object", "properties": {
        "keys": {"type": "array", "items": {"type": "string"},
                 "description": "Only these nodes. Omit to read every node."}},
        "additionalProperties": False},
}, _unsupported, opt_in=True))
register(Tool(TOOL_NAMES[1], {
    "name": TOOL_NAMES[1],
    "description": "Create or update blueprint nodes. The key is the declaration's short name (snake_case for lemmas and theorems, camelCase for definitions). The system assigns the Lean name `<namespace>.<key>` and a file; declare exactly that name. Give every node a one or two sentence statement and list the keys it depends on in `uses` (omit for leaves). Never write status. Send all nodes of a plan in one call.",
    "input_schema": {"type": "object", "properties": {
        "nodes": {"type": "array", "minItems": 1, "maxItems": 100, "items": {
            "type": "object", "properties": {
                "key": {"type": "string"},
                "kind": {"type": "string", "enum": ["theorem", "lemma", "definition"]},
                "statement": {"type": "string"},
                "uses": {"type": "array", "items": {"type": "string"}},
                "lean": {"type": "string"}},
            "required": ["key"], "additionalProperties": False}}},
        "required": ["nodes"], "additionalProperties": False},
}, _unsupported, opt_in=True))
register(Tool(TOOL_NAMES[2], {
    "name": TOOL_NAMES[2],
    "description": "Remove blueprint nodes by key. Refused when other nodes depend on them unless cascade is true.",
    "input_schema": {"type": "object", "properties": {
        "keys": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "cascade": {"type": "boolean"}},
        "required": ["keys"], "additionalProperties": False},
}, _unsupported, opt_in=True))
