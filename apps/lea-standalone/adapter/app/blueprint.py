"""Blueprint parsing, validation and canonical rendering (D28/D29, PLAN-blueprint-tools §2.1).

`.lea/blueprint.md` is the project's proof decomposition. The system writes it
through :mod:`blueprint_store` (the agent's blueprint tools and the review/accept
routes), always as :func:`render` output; humans may still edit the markdown, so
the parser is tolerant: a malformed section degrades to an advisory warning and
never fails the whole document.

Canonical form (what :func:`render` writes):

    # Blueprint — <title>

    <preamble prose, verbatim, optional>

    ## <key>
    - kind: theorem | lemma | definition
    - lean: `Lea.Ns.key`          (omitted when unknown)
    - file: `Path/In/Repo.lean`   (omitted when unknown)
    - uses: a, b                  (omitted when empty)

    <statement prose or LaTeX, verbatim>

A ``## `` section is a *node* iff its header block (the run of ``- word: value``
lines directly after the heading, leading blank lines skipped) has at least one
recognised key (``kind``, ``lean``, ``file``, ``uses``). Any other ``## `` section
is a *prose section*, kept verbatim and in place. Lines inside a ``` fence are
inert. Status is never in the file: :mod:`graph` derives it from live Lean state.

Fixtures shared with the UI parser: ``apps/lea-standalone/fixtures/blueprint``.
"""

from __future__ import annotations

import re

# The shapes a node's `kind` may take (D28: definition=box · lemma/theorem=ellipse).
VALID_KINDS = ("definition", "lemma", "theorem")
# Header keys the parser recognises, in canonical render order.
HEADER_KEYS = ("kind", "lean", "file", "uses")
# A node key is the declaration's short name.
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*$")
MAX_KEY_LENGTH = 64
# A `uses:` value that is exactly one of these tokens means "no dependencies"
# (unless a node actually has that key).
USES_SENTINELS = frozenset({"none", "-", "—", "–", "n/a", "nothing"})

_HEADING_RE = re.compile(r"^##[ \t]+(\S.*?)\s*$")
_HEADER_RE = re.compile(r"^\s*-\s*([A-Za-z][A-Za-z0-9_-]*)\s*:(.*)$")
_FENCE_RE = re.compile(r"^\s*```")
_BACKTICK_RE = re.compile(r"`([^`]*)`")
_TRAILING_PUNCTUATION = ",;:.)"


# ── value extraction ──────────────────────────────────────────────────────────


def _extract_name(value: str) -> str | None:
    """`lean:`/`file:` value: the first backtick span if any, else the first
    whitespace token with trailing ``,;:.)`` stripped. Empty → ``None``."""
    span = _BACKTICK_RE.search(value)
    if span:
        name = span.group(1).strip()
        return name or None
    tokens = value.split()
    if not tokens:
        return None
    name = tokens[0].rstrip(_TRAILING_PUNCTUATION)
    return name or None


def _split_uses(value: str) -> list[str]:
    """`uses:` value: split on commas and whitespace, strip backticks."""
    return [token for token in (part.strip("`") for part in re.split(r"[,\s]+", value)) if token]


def is_valid_key(key: str) -> bool:
    return bool(KEY_RE.match(key or "")) and len(key) <= MAX_KEY_LENGTH


# ── text helpers ──────────────────────────────────────────────────────────────


def _lines(text: str) -> list[str]:
    """Split on newlines only (``str.splitlines`` also splits on U+2028 and
    friends, which can occur inside statement prose)."""
    return (text or "").replace("\r\n", "\n").split("\n")


def _trim_lines(lines: list[str]) -> list[str]:
    """Drop blank lines at both ends; everything in between stays verbatim."""
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _trim(text: str) -> str:
    return "\n".join(_trim_lines(_lines(text)))


def _close_fences(text: str) -> str:
    """Close a dangling ``` fence so the sections rendered after it stay live."""
    fences = sum(1 for line in _lines(text) if _FENCE_RE.match(line))
    return f"{text}\n```" if fences % 2 else text


def _warning(code: str, node: str | None, message: str) -> dict:
    return {"code": code, "node": node, "message": message}


# ── parse ─────────────────────────────────────────────────────────────────────


def _sections(text: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Split into the preamble and ``(heading, lines)`` sections, fence-aware."""
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current: list[str] = preamble
    in_fence = False
    for line in _lines(text):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            heading = _HEADING_RE.match(line)
            if heading:
                current = []
                sections.append((heading.group(1).strip(), current))
                continue
        current.append(line)
    return preamble, sections


def _section_block(heading: str, lines: list[str], warnings: list[dict]) -> dict:
    """One ``## `` section → a node (recognised header present) or a prose block."""
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    headers: list[tuple[str, str]] = []
    while index < len(lines):
        match = _HEADER_RE.match(lines[index])
        if not match:
            break
        headers.append((match.group(1).lower(), match.group(2)))
        index += 1

    if not any(name in HEADER_KEYS for name, _ in headers):
        return {"type": "prose", "heading": heading, "body": "\n".join(_trim_lines(lines))}

    node = {"type": "node", "key": heading, "kind": None, "lean": None, "file": None,
            "uses": [], "statement": "\n".join(_trim_lines(lines[index:]))}
    raw_uses: list[str] = []
    for name, value in headers:
        if name == "kind":
            node["kind"] = value.strip().lower() or None
        elif name == "lean":
            node["lean"] = _extract_name(value)
        elif name == "file":
            node["file"] = _extract_name(value)
        elif name == "uses":
            raw_uses = _split_uses(value)
        else:
            warnings.append(_warning(
                "unknown_header", heading,
                f"node `{heading}` has an unknown header `- {name}:`; it was dropped "
                "(recognised: kind, lean, file, uses)."))
    node["uses"] = raw_uses
    return node


def find_cycles(adjacency: dict[str, list[str]]) -> set[str]:
    """Keys that lie on a dependency cycle of two or more nodes (Tarjan SCC).

    Self-loops are not cycles here; they are reported as ``self_dependency``.
    Targets that are not keys of ``adjacency`` are ignored."""
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cyclic: set[str] = set()
    counter = 0

    for root in adjacency:
        if root in index_of:
            continue
        # Iterative DFS: (node, iterator over its successors).
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(adjacency.get(root, ())))]
        while work:
            node, successors = work[-1]
            advanced = False
            for succ in successors:
                if succ == node or succ not in adjacency:
                    continue
                if succ not in index_of:
                    index_of[succ] = low[succ] = counter
                    counter += 1
                    stack.append(succ)
                    on_stack.add(succ)
                    work.append((succ, iter(adjacency.get(succ, ()))))
                    advanced = True
                    break
                if succ in on_stack:
                    low[node] = min(low[node], index_of[succ])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    cyclic.update(component)
    return cyclic


def node_warnings(node: dict) -> list[dict]:
    """Per-node checks: ``invalid_key``, ``missing_kind``, ``unknown_kind``."""
    key = node["key"]
    warnings: list[dict] = []
    if not is_valid_key(key):
        warnings.append(_warning(
            "invalid_key", key,
            f"`{key}` is not a valid node key (a declaration short name: letters, digits, "
            f"`_` and `'`, starting with a letter or `_`, at most {MAX_KEY_LENGTH} characters)."))
    if node.get("kind") is None:
        warnings.append(_warning("missing_kind", key, f"node `{key}` has no `kind:` (definition | lemma | theorem)."))
    elif node["kind"] not in VALID_KINDS:
        warnings.append(_warning(
            "unknown_kind", key,
            f"node `{key}` has unrecognised kind `{node['kind']}` (use definition | lemma | theorem)."))
    return warnings


def document_warnings(nodes: list[dict]) -> list[dict]:
    """Document-level checks: ``duplicate_key``, ``dangling_dependency``,
    ``self_dependency``, ``cycle`` (one per node on a cycle of two or more)."""
    warnings: list[dict] = []
    keys = {node["key"] for node in nodes}
    seen: set[str] = set()
    for node in nodes:
        key = node["key"]
        if key in seen:
            warnings.append(_warning("duplicate_key", key, f"duplicate node key `{key}`; keys must be unique."))
        seen.add(key)

    adjacency: dict[str, list[str]] = {}
    for node in nodes:
        key = node["key"]
        adjacency.setdefault(key, []).extend(node["uses"])
        for dep in node["uses"]:
            if dep == key:
                warnings.append(_warning("self_dependency", key, f"node `{key}` uses itself."))
            elif dep not in keys:
                warnings.append(_warning(
                    "dangling_dependency", key,
                    f"node `{key}` uses `{dep}`, which is not a node in this blueprint."))
    cyclic = find_cycles(adjacency)
    reported: set[str] = set()
    for node in nodes:
        key = node["key"]
        if key in cyclic and key not in reported:
            reported.add(key)
            warnings.append(_warning("cycle", key, f"node `{key}` is on a dependency cycle."))
    return warnings


def parse(text: str) -> dict:
    """Parse blueprint markdown.

    Returns ``{"preamble", "blocks", "nodes", "edges", "warnings"}``:

    - ``preamble``: text before the first ``## `` heading (title included), trimmed
      of blank lines at both ends.
    - ``blocks``: the sections in file order, each a node
      ``{"type": "node", "key", "kind", "lean", "file", "uses", "statement"}`` or a
      prose section ``{"type": "prose", "heading", "body"}``.
    - ``nodes``: the node blocks (the same dicts), in file order. ``kind``, ``lean``
      and ``file`` are ``None`` when absent; ``uses`` is ``[]`` when empty.
    - ``edges``: ``{"from": key, "to": dep}`` per ``uses`` entry, as declared.
    - ``warnings``: advisory ``{"code", "node", "message"}`` dicts (never raises).
    """
    preamble_lines, sections = _sections(text)
    warnings: list[dict] = []
    blocks = [_section_block(heading, lines, warnings) for heading, lines in sections]
    nodes = [block for block in blocks if block["type"] == "node"]

    keys = {node["key"] for node in nodes}
    for node in nodes:
        uses = node["uses"]
        if len(uses) == 1 and uses[0].lower() in USES_SENTINELS and uses[0] not in keys:
            node["uses"] = []

    # Per-node warnings first (with any unknown_header already recorded), then the
    # document-level ones.
    ordered: list[dict] = []
    for node in nodes:
        ordered.extend(node_warnings(node))
    ordered.extend(warnings)
    ordered.extend(document_warnings(nodes))

    edges = [{"from": node["key"], "to": dep} for node in nodes for dep in node["uses"]]
    return {
        "preamble": "\n".join(_trim_lines(preamble_lines)),
        "blocks": blocks,
        "nodes": nodes,
        "edges": edges,
        "warnings": ordered,
    }


def validate(text: str) -> list[dict]:
    """The advisory warnings of :func:`parse` (never raises)."""
    return parse(text)["warnings"]


# ── render ────────────────────────────────────────────────────────────────────


def empty_document(title: str) -> str:
    """The canonical empty blueprint a new project is seeded with."""
    return (
        f"# Blueprint — {title}\n\n"
        "Lea maintains the nodes of this blueprint through its blueprint tools; "
        "you can also edit the markdown directly.\n"
    )


def render_node(node: dict) -> str:
    """One canonical ``## `` node section (no trailing newline)."""
    lines = [f"## {node['key']}"]
    if node.get("kind"):
        lines.append(f"- kind: {node['kind']}")
    if node.get("lean"):
        lines.append(f"- lean: `{node['lean']}`")
    if node.get("file"):
        lines.append(f"- file: `{node['file']}`")
    if node.get("uses"):
        lines.append(f"- uses: {', '.join(node['uses'])}")
    if len(lines) == 1:
        # Keep a header line so the section still parses as a node, not prose.
        lines.append("- kind:")
    text = "\n".join(lines)
    statement = _trim(node.get("statement") or "")
    if statement:
        text += "\n\n" + _close_fences(statement)
    return text


def render(parsed: dict) -> str:
    """Canonical markdown for a parsed (or programmatically built) document.

    Uses ``parsed["blocks"]`` when present, else ``parsed["nodes"]``. One blank
    line between blocks, exactly one trailing newline (``""`` for an empty
    document). Idempotent: ``render(parse(render(parse(t)))) == render(parse(t))``."""
    parts: list[str] = []
    preamble = _trim(parsed.get("preamble") or "")
    if preamble:
        parts.append(_close_fences(preamble))
    blocks = parsed.get("blocks")
    if blocks is None:
        blocks = parsed.get("nodes") or []
    for block in blocks:
        if block.get("type") == "prose":
            body = _trim(block.get("body") or "")
            section = f"## {block['heading']}"
            if body:
                section += "\n\n" + _close_fences(body)
            parts.append(section)
        else:
            parts.append(render_node(block))
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"
