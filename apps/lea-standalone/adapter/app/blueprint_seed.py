"""Generate blueprint nodes from formalized artifacts (FEATURE-overleaf-blueprint-view).

The blueprint graph is derived from ``.lea/blueprint.md`` (:mod:`blueprint` parses it,
:mod:`graph` enriches it). Nothing writes that file automatically — so a project that
was *formalized* (real ``.lean`` proofs, recorded in the ``artifacts`` index) but never
*decomposed* has an empty graph. This module closes that gap on demand: it reads the
project's recorded artifacts and appends a ``## node`` section per formalized
declaration, so the graph populates from what actually exists.

Design:

- **Source of truth is the artifacts index** (``store.list_artifacts_for_scope``) — the
  decls the run finalizer recorded, i.e. "what you formalized". One node per artifact.
- **Additive + idempotent.** A node is emitted only for an artifact not already
  represented (matched by ``lean:`` FQN), and existing content is never modified or
  removed. Safe to re-run after formalizing more theorems; never clobbers hand/agent
  authoring done in the standalone UI.
- **Edges are derived**, not invented: a new node ``uses`` another formalized decl iff
  its own decl span textually references that decl's short name (reusing the same
  decl-span scan :mod:`graph` uses for its ``sorry`` check).

The status/color of the generated nodes is *not* written here — it stays derived from
live Lean state by :mod:`graph`, exactly as for hand-authored nodes.
"""

from __future__ import annotations

import re

from . import blueprint
from . import graph
from . import projects as project_service
from . import store
from .artifacts import scan_lean_declarations

# Lean decl keyword → blueprint kind (definition = box, lemma/theorem = ellipse).
_KEYWORD_KIND = {
    "theorem": "theorem",
    "lemma": "lemma",
    "def": "definition",
    "abbrev": "definition",
    "instance": "definition",
    "structure": "definition",
    "inductive": "definition",
    "class": "definition",
}

# Cap the auto-extracted signature used as node statement prose.
_SIGNATURE_CAP = 240
_SIGNATURE_DECL_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)?"
    r"(?:private\s+|protected\s+|noncomputable\s+|unsafe\s+|partial\s+|"
    r"scoped\s+|local\s+)*"
    r"(?:theorem|lemma|def|abbrev|structure|class|inductive|coinductive|instance|opaque)\s+"
    r"[A-Za-z_][A-Za-z0-9_'.]*"
)


def _unique_key(base: str, taken: set[str]) -> str:
    """A blueprint key that doesn't collide with one already in the file/batch."""
    key = base or "node"
    n = 2
    while key in taken:
        key = f"{base}_{n}"
        n += 1
    taken.add(key)
    return key


def _decl_span_and_keyword(text: str, short: str) -> tuple[str | None, str]:
    """``(keyword, span_text)`` for the decl named ``short`` in ``text`` — the header
    line's Lean keyword and that decl's span (header to the next decl header/EOF).
    ``(None, "")`` when the decl isn't found in the text."""
    if not text:
        return None, ""
    matches = [decl for decl in scan_lean_declarations(text) if decl.short_name == short]
    if len(matches) != 1:
        return None, ""
    return matches[0].keyword, matches[0].span


def _signature(span_text: str) -> str:
    """The decl header from after its name up to ``:=`` (binders + type), as the node's
    statement. The source's own line breaks are **preserved** so a multi-line signature
    reads the same here as in the Lean pane; only trailing per-line spaces, blank outer
    lines, and the gap after the decl name are trimmed. Capped. ``""`` when unusable."""
    if not span_text:
        return ""
    head = span_text.split(":=", 1)[0]
    decl = _SIGNATURE_DECL_RE.match(head)
    if decl:
        head = head[decl.end():]
    lines = [line.rstrip() for line in head.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if lines:
        lines[0] = lines[0].lstrip()  # drop the space between the decl name and its binders
    return "\n".join(lines)[:_SIGNATURE_CAP].rstrip()


def _strip_lean_comments(text: str) -> str:
    """Remove Lean comments — ``--`` to end of line and (nesting) ``/- … -/`` blocks —
    so decl references used for edge derivation aren't matched inside a comment. Best
    effort: it doesn't model string literals (a decl name inside a string is rare in
    proof terms and low-risk)."""
    out: list[str] = []
    i, n, depth = 0, len(text), 0
    while i < n:
        pair = text[i:i + 2]
        if depth:
            if pair == "/-":
                depth += 1
                i += 2
            elif pair == "-/":
                depth -= 1
                i += 2
            else:
                i += 1
            continue
        if pair == "/-":
            depth += 1
            i += 2
        elif pair == "--":
            nl = text.find("\n", i)
            if nl == -1:
                break
            i = nl  # keep the newline so line structure survives
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _kind_for(keyword: str | None, artifact_kind: str | None) -> str:
    """Blueprint kind from the Lean keyword, falling back to the artifact's kind."""
    if keyword and keyword in _KEYWORD_KIND:
        return _KEYWORD_KIND[keyword]
    return "definition" if artifact_kind == "definition" else "lemma"


def _render_block(node: dict) -> str:
    """One ``## `` blueprint section for a synthesized node."""
    lines = [f"## {node['key']}", f"- kind: {node['kind']}", f"- lean: `{node['lean']}`"]
    if node["uses"]:
        lines.append(f"- uses: {', '.join(node['uses'])}")
    lines.append("")
    lines.append(node["statement"])
    return "\n".join(lines)


def generate(project: dict, proofs_root) -> dict:
    """Append blueprint nodes for the project's formalized artifacts (additive,
    idempotent). Writes + commits ``.lea/blueprint.md`` when anything is added.

    Returns ``{"added": int, "skipped": int, "warnings": [str], "graph": {...}}`` —
    ``skipped`` counts artifacts already represented; ``graph`` is the freshly derived
    graph so a caller can render in one round-trip."""
    repo = project_service.project_repo_dir(project, proofs_root)
    artifacts = store.list_artifacts_for_scope(project["id"])

    existing_text = project_service.read_doc(project, proofs_root, "blueprint.md")
    parsed = blueprint.parse(existing_text)
    existing_nodes = parsed["nodes"]
    existing_leans = {n["lean"] for n in existing_nodes if n["lean"]}
    taken_keys = {n["key"] for n in existing_nodes}

    _fqn_to_file, file_to_text = graph._scan_lean_decls(repo)

    warnings: list[str] = []
    candidates: list[dict] = []
    skipped = 0
    for art in artifacts:
        fqn = art["declaration_name"]
        if not fqn or fqn in existing_leans:
            skipped += 1
            continue
        short = graph._short(fqn)
        text = file_to_text.get(art.get("path") or "", "")
        keyword, span = _decl_span_and_keyword(text, short)
        if keyword is None:
            warnings.append(f"`{fqn}` recorded but not found in a .lean file — node added without kind/edges.")
        candidates.append({
            "key": _unique_key(short, taken_keys),
            "lean": fqn,
            "short": short,
            "kind": _kind_for(keyword, art.get("kind")),
            "statement": _signature(span) or f"Formalized as `{fqn}`.",
            "span": span,
        })

    # Derive `uses` edges: a candidate depends on any formalized decl (existing node
    # or sibling candidate) whose short name its own span references. Keyed by the
    # target node's blueprint key so the edge resolves in the graph.
    # A short name may map to several decls (same last component in different
    # namespaces); such references are ambiguous, so we skip them rather than guess
    # (better a missing edge than a wrong one). Unambiguous shorts (one owner) edge.
    short_to_keys: dict[str, list[str]] = {}
    for node in existing_nodes:
        if node["lean"]:
            short_to_keys.setdefault(graph._short(node["lean"]), []).append(node["key"])
    for cand in candidates:
        short_to_keys.setdefault(cand["short"], []).append(cand["key"])

    for cand in candidates:
        uses: list[str] = []
        seen: set[str] = set()
        # Scan the comment-stripped span so a decl name mentioned only in a comment
        # doesn't produce a phantom dependency.
        scan = _strip_lean_comments(cand["span"])
        for other_short, keys in short_to_keys.items():
            if other_short == cand["short"] or len(keys) != 1:
                continue
            other_key = keys[0]
            if other_key == cand["key"] or other_key in seen:
                continue
            if re.search(rf"\b{re.escape(other_short)}\b", scan):
                seen.add(other_key)
                uses.append(other_key)
        cand["uses"] = uses

    if candidates:
        blocks = "\n\n".join(_render_block(c) for c in candidates)
        # If the existing content ends inside an unclosed ``` fence (an odd number of
        # fence lines), close it first — otherwise the appended `## ` sections land
        # inside the fence, parse as inert, and get re-added on every run (breaking the
        # idempotency guarantee). Balanced fences (the normal case) add nothing.
        fences = sum(1 for line in existing_text.splitlines() if blueprint._FENCE_RE.match(line))
        closer = "```\n\n" if fences % 2 == 1 else ""
        new_text = f"{existing_text.rstrip()}\n\n{closer}{blocks}\n"
        project_service.write_doc(project, proofs_root, "blueprint.md", new_text)

    return {
        "added": len(candidates),
        "skipped": skipped,
        "warnings": warnings,
        "graph": graph.build_graph(project, proofs_root),
    }
